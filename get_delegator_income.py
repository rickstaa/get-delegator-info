"""Retrieve and export delegator income data for tax reporting as a CSV file."""

import sys
from datetime import datetime, timezone

from gql import gql
from web3 import Web3
import pandas as pd
from pandas import ExcelWriter
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from tabulate import tabulate
from tqdm import tqdm

from get_orch_income import (
    add_cumulative_balances,
    fetch_crypto_price,
    human_to_unix_time,
    fetch_block_number_by_timestamp,
    fetch_starting_eth_balance,
    fetch_starting_lpt_balance,
    fetch_block_hash_for_round,
    fetch_all_transactions,
    fetch_pending_fees,
    fetch_pending_stake,
    retrieve_token_and_eth_transfers,
    fetch_bond_events,
    fetch_unbond_events,
    fetch_transfer_bond_events,
    process_bond_events,
    process_unbond_events,
    process_transfer_bond_events,
    fetch_and_process_events,
    BONDING_MANAGER_CONTRACT,
    GRAPHQL_CLIENT,
)

tqdm.pandas()

ROUNDS_QUERY = """
query Rounds($first: Int!, $skip: Int!, $startTimestamp_gt: Int!, $startTimestamp_lt: Int!) {
  rounds(
    where: { startTimestamp_gt: $startTimestamp_gt, startTimestamp_lt: $startTimestamp_lt }
    first: $first
    skip: $skip
    orderBy: startTimestamp
    orderDirection: asc
  ) {
    id
    startTimestamp
    startBlock
  }
}
"""

RPC_HISTORY_ERROR_DISPLAYED = False


def get_csv_column_order(currency: str) -> list:
    """Generate the CSV column order with dynamic currency names.

    Args:
        currency: The currency to use for the report.

    Returns:
        A list of CSV column names.
    """
    return [
        "timestamp",
        "round",
        "transaction hash",
        "transaction url",
        "direction",
        "transaction type",
        "currency",
        "amount",
        f"value ({currency})",
        f"price ({currency})",
        "pending rewards",
        "pending fees",
        "accumulated rewards",
        "accumulated fees",
        "source function",
        "cumulative balance (ETH)",
        "cumulative balance (LPT)",
    ]


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    retry=retry_if_exception_type(Exception),
)
def fetch_delegator_info(delegator: str, block_hash: str) -> dict:
    """Fetch comprehensive delegator information at a specific block.

    Args:
        delegator: The address of the delegator.
        block_hash: The block hash to fetch the info at.

    Returns:
        A dictionary with delegator information.
    """
    try:
        checksum_delegator = Web3.to_checksum_address(delegator)

        # Fetch general delegator info.
        delegator_info = BONDING_MANAGER_CONTRACT.functions.getDelegator(
            checksum_delegator
        ).call(block_identifier=block_hash)
        bonded_amount = delegator_info[0] / 10**18
        fees = delegator_info[1] / 10**18
        delegate_address = delegator_info[2]
        delegated_amount = delegator_info[3] / 10**18
        start_round = delegator_info[4]
        last_claim_round = delegator_info[5]
        next_unbonding_lock_id = delegator_info[6]

        # Get pending stake and rewards using the retry functions.
        pending_stake = fetch_pending_stake(address=delegator, block_hash=block_hash)
        pending_fees = fetch_pending_fees(address=delegator, block_hash=block_hash)

        return {
            "bonded_amount": bonded_amount,
            "fees": fees,
            "delegate_address": delegate_address,
            "delegated_amount": delegated_amount,
            "start_round": start_round,
            "last_claim_round": last_claim_round,
            "next_unbonding_lock_id": next_unbonding_lock_id,
            "pending_stake": pending_stake,
            "pending_fees": pending_fees,
        }
    except Exception as e:
        print(f"Error fetching delegator info for {delegator}: {e}")
        return None


def fetch_rounds_in_timeframe(start_timestamp: int, end_timestamp: int) -> list:
    """Fetch all rounds within a timestamp range.

    Args:
        start_timestamp: The start timestamp of the range.
        end_timestamp: The end timestamp of the range.

    Returns:
        A list of rounds within the specified timestamp range.
    """
    variables = {
        "first": 1000,
        "skip": 0,
        "startTimestamp_gt": start_timestamp,
        "startTimestamp_lt": end_timestamp,
    }
    all_rounds = []
    while True:
        try:
            response = GRAPHQL_CLIENT.execute(
                gql(ROUNDS_QUERY), variable_values=variables
            )
            rounds = response.get("rounds", [])
            all_rounds.extend(rounds)

            if len(rounds) < variables["first"]:
                break
            variables["skip"] += variables["first"]
        except Exception as e:
            print(f"Error fetching rounds: {e}")
            break
    return all_rounds


def process_delegator_balances_over_rounds(
    delegator: str,
    rounds: list,
    currency: str,
    starting_pending_stake: float,
    starting_pending_fees: float,
) -> pd.DataFrame:
    """Process delegator balances over rounds using pendingStake and pendingFees.

    Args:
        delegator: The delegator address to process.
        rounds: A list of rounds to process.
        currency: The currency for price conversion.
        starting_pending_stake: The initial pending stake to subtract from totals.
        starting_pending_fees: The initial pending fees to subtract from totals.

    Returns:
        A DataFrame containing the processed delegator balances over rounds.
    """
    rows = []
    previous_pending_stake = starting_pending_stake
    previous_pending_fees = starting_pending_fees
    for round_data in tqdm(rounds, desc="Processing rounds for delegator balances"):
        round_id = round_data["id"]
        unix_timestamp = round_data["startTimestamp"]

        # Retrieve pending stake and fees for the delegator at the round.
        block_hash = fetch_block_hash_for_round(round_number=round_id)
        if not block_hash:
            continue
        delegator_info = fetch_delegator_info(delegator, block_hash)
        if not delegator_info:
            continue
        timestamp = datetime.fromtimestamp(unix_timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        current_pending_stake = delegator_info["pending_stake"]
        current_pending_fees = delegator_info["pending_fees"]

        # Calculate accumulated income since start.
        accumulated_rewards = max(0, current_pending_stake - starting_pending_stake)
        accumulated_fees = max(0, current_pending_fees - starting_pending_fees)
        reward_income = max(0, current_pending_stake - previous_pending_stake)
        fee_income = max(0, current_pending_fees - previous_pending_fees)

        # Add rows for round income if they are greater than zero.
        base_row = {
            "timestamp": timestamp,
            "round": round_id,
            "transaction hash": "",
            "transaction url": "",
            "direction": "incoming",
            "pending rewards": current_pending_stake,
            "pending fees": current_pending_fees,
            "accumulated rewards": accumulated_rewards,
            "accumulated fees": accumulated_fees,
        }
        if reward_income > 0:
            lpt_price = fetch_crypto_price("LPT", currency, unix_timestamp)
            reward_row = base_row.copy()
            reward_row.update(
                {
                    "transaction type": "pending rewards",
                    "currency": "LPT",
                    "amount": reward_income,
                    f"price ({currency})": lpt_price,
                    f"value ({currency})": reward_income * lpt_price,
                    "source function": "pendingStake",
                }
            )
            rows.append(reward_row)
        if fee_income > 0:
            eth_price = fetch_crypto_price("ETH", currency, unix_timestamp)
            fee_row = base_row.copy()
            fee_row.update(
                {
                    "transaction type": "pending fees",
                    "currency": "ETH",
                    "amount": fee_income,
                    f"price ({currency})": eth_price,
                    f"value ({currency})": fee_income * eth_price,
                    "source function": "pendingFees",
                }
            )
            rows.append(fee_row)

        previous_pending_stake = current_pending_stake
        previous_pending_fees = current_pending_fees
    return pd.DataFrame(rows)


def generate_overview_table(
    delegator: str,
    start_time: str,
    end_time: str,
    reward_data: pd.DataFrame,
    fee_data: pd.DataFrame,
    currency: str,
    starting_eth_balance: float,
    starting_eth_value: float,
    starting_lpt_balance: float,
    starting_lpt_value: float,
    end_eth_balance: float,
    end_eth_value: float,
    end_lpt_balance: float,
    end_lpt_value: float,
    starting_pending_stake: float,
    starting_pending_fees: float,
    start_lpt_price: float,
    start_eth_price: float,
    end_lpt_price: float,
    end_eth_price: float,
) -> list:
    """Generate an overview table with key metrics for delegator.

    Args:
        delegator: The delegator address.
        start_time: The start time of the report.
        end_time: The end time of the report.
        reward_data: DataFrame containing pending rewards data.
        fee_data: DataFrame containing pending fees data.
        currency: The currency for the report.
        starting_eth_balance: Starting ETH balance.
        starting_eth_value: Starting ETH value in the specified currency.
        starting_lpt_balance: Starting LPT balance.
        starting_lpt_value: Starting LPT value in the specified currency.
        end_eth_balance: Ending ETH balance.
        end_eth_value: Ending ETH value in the specified currency.
        end_lpt_balance: Ending LPT balance.
        end_lpt_value: Ending LPT value in the specified currency.
        starting_pending_stake: Starting pending stake amount.
        starting_pending_fees: Starting pending fees amount.
        start_lpt_price: Starting LPT price in the specified currency.
        start_eth_price: Starting ETH price in the specified currency.
        end_lpt_price: Ending LPT price in the specified currency.
        end_eth_price: Ending ETH price in the specified currency.

    Returns:
        A list of lists representing the overview table rows.
    """
    # Get accumulated values.
    total_accumulated_rewards = (
        reward_data.get("accumulated rewards", pd.Series(0)).max()
        if not reward_data.empty
        else 0
    )
    total_accumulated_fees = (
        fee_data.get("accumulated fees", pd.Series(0)).max()
        if not fee_data.empty
        else 0
    )

    # Calculate values for accumulated amounts.
    latest_reward_price = (
        reward_data.get(f"price ({currency})", pd.Series(0)).iloc[-1]
        if not reward_data.empty
        else end_lpt_price
    )
    latest_fee_price = (
        fee_data.get(f"price ({currency})", pd.Series(0)).iloc[-1]
        if not fee_data.empty
        else end_eth_price
    )
    total_accumulated_reward_value = total_accumulated_rewards * latest_reward_price
    total_accumulated_fees_value = total_accumulated_fees * latest_fee_price
    total_value_accumulated = (
        total_accumulated_reward_value + total_accumulated_fees_value
    )

    # Get pending values.
    end_pending_rewards = (
        reward_data.get("pending rewards", pd.Series(0)).iloc[-1]
        if not reward_data.empty
        else 0
    )
    end_pending_fees = (
        fee_data.get("pending fees", pd.Series(0)).iloc[-1] if not fee_data.empty else 0
    )
    starting_pending_stake_value = starting_pending_stake * start_lpt_price
    starting_pending_fees_value = starting_pending_fees * start_eth_price
    total_pending_rewards_value = end_pending_rewards * end_lpt_price
    total_pending_fees_value = end_pending_fees * end_eth_price

    overview_table = [
        ["Network", "Arbitrum"],
        ["Delegator Address", delegator],
        ["Start Time", start_time],
        ["End Time", end_time],
        [
            "Starting ETH Balance",
            f"{starting_eth_balance:.4f} ETH ({starting_eth_value:.2f} {currency})",
        ],
        [
            "Starting LPT Balance",
            f"{starting_lpt_balance:.4f} LPT ({starting_lpt_value:.2f} {currency})",
        ],
        [
            "Starting Pending Stake",
            f"{starting_pending_stake:.4f} LPT ({starting_pending_stake_value:.2f} {currency})",
        ],
        [
            "Starting Pending Fees",
            f"{starting_pending_fees:.4f} ETH ({starting_pending_fees_value:.2f} {currency})",
        ],
        [
            "Ending ETH Balance",
            f"{end_eth_balance:.4f} ETH ({end_eth_value:.2f} {currency})",
        ],
        [
            "Ending LPT Balance",
            f"{end_lpt_balance:.4f} LPT ({end_lpt_value:.2f} {currency})",
        ],
        [
            "Ending Pending Stake",
            f"{end_pending_rewards:.4f} LPT ({total_pending_rewards_value:.2f} {currency})",
        ],
        [
            "Ending Pending Fees",
            f"{end_pending_fees:.4f} ETH ({total_pending_fees_value:.2f} {currency})",
        ],
        ["Accumulated Rewards (LPT)", f"{total_accumulated_rewards:.4f} LPT"],
        [
            f"Accumulated Rewards ({currency})",
            f"{total_accumulated_reward_value:.4f} {currency}",
        ],
        ["Accumulated Fees (ETH)", f"{total_accumulated_fees:.4f} ETH"],
        [
            f"Accumulated Fees ({currency})",
            f"{total_accumulated_fees_value:.4f} {currency}",
        ],
        [
            f"Total Value Accumulated ({currency})",
            f"{total_value_accumulated:.4f} {currency}",
        ],
    ]
    return overview_table


if __name__ == "__main__":
    print("== Delegator Income Data Exporter ==")

    start_time = input("Enter data range start (YYYY-MM-DD HH:MM:SS): ").strip()
    start_timestamp = human_to_unix_time(human_time=start_time)
    end_time = input("Enter data range end (YYYY-MM-DD HH:MM:SS): ").strip()
    end_timestamp = human_to_unix_time(human_time=end_time)
    delegator = input("Enter delegator address: ").strip().lower()
    if not delegator:
        print("Delegator address is required.")
        sys.exit(1)
    currency = input("Enter currency (default: EUR): ").strip().upper() or "EUR"

    print("\nFetching start and end balances...")
    start_block_number = fetch_block_number_by_timestamp(timestamp=start_timestamp)
    end_block_number = fetch_block_number_by_timestamp(timestamp=end_timestamp)
    starting_eth_balance = fetch_starting_eth_balance(
        wallet_address=delegator, block_hash=start_block_number
    )
    starting_lpt_balance = fetch_starting_lpt_balance(
        wallet_address=delegator, block_hash=start_block_number
    )
    end_eth_balance = fetch_starting_eth_balance(
        wallet_address=delegator, block_hash=end_block_number
    )
    end_lpt_balance = fetch_starting_lpt_balance(
        wallet_address=delegator, block_hash=end_block_number
    )
    start_eth_price = fetch_crypto_price(
        crypto_symbol="ETH", target_currency=currency, unix_timestamp=start_timestamp
    )
    start_lpt_price = fetch_crypto_price(
        crypto_symbol="LPT", target_currency=currency, unix_timestamp=start_timestamp
    )
    end_eth_price = fetch_crypto_price(
        crypto_symbol="ETH", target_currency=currency, unix_timestamp=end_timestamp
    )
    end_lpt_price = fetch_crypto_price(
        crypto_symbol="LPT", target_currency=currency, unix_timestamp=end_timestamp
    )
    starting_eth_value = starting_eth_balance * start_eth_price
    starting_lpt_value = starting_lpt_balance * start_lpt_price
    end_eth_value = end_eth_balance * end_eth_price
    end_lpt_value = end_lpt_balance * end_lpt_price

    print(f"\nFetching rounds in timeframe...")
    rounds = fetch_rounds_in_timeframe(start_timestamp, end_timestamp)
    print(f"Found {len(rounds)} rounds in timeframe.")

    print("\nFetching start and end pending balances...\n")
    start_round = rounds[0]["id"] if rounds else None
    starting_delegator_info = (
        fetch_delegator_info(delegator, start_block_number) if start_round else None
    )
    starting_pending_stake = (
        starting_delegator_info["pending_stake"] if starting_delegator_info else 0
    )
    starting_pending_fees = (
        starting_delegator_info["pending_fees"] if starting_delegator_info else 0
    )
    ending_delegator_info = fetch_delegator_info(delegator, end_block_number)
    ending_pending_stake = (
        ending_delegator_info["pending_stake"] if ending_delegator_info else 0
    )
    ending_pending_fees = (
        ending_delegator_info["pending_fees"] if ending_delegator_info else 0
    )

    print("\nWallet balances:")
    print(
        f"Starting ETH Balance: {starting_eth_balance:.4f} ETH ({starting_eth_value:.2f} {currency})"
    )
    print(
        f"Ending ETH Balance: {end_eth_balance:.4f} ETH ({end_eth_value:.2f} {currency})"
    )
    print(
        f"Starting LPT Balance: {starting_lpt_balance:.4f} LPT ({starting_lpt_value:.2f} {currency})"
    )
    print(
        f"Ending LPT Balance: {end_lpt_balance:.4f} LPT ({end_lpt_value:.2f} {currency})"
    )

    # Calculate values for pending amounts
    starting_pending_stake_value = starting_pending_stake * start_lpt_price
    starting_pending_fees_value = starting_pending_fees * start_eth_price
    ending_pending_stake_value = ending_pending_stake * end_lpt_price
    ending_pending_fees_value = ending_pending_fees * end_eth_price

    print("\nStaking information:")
    print(
        f"Starting pending stake: {starting_pending_stake:.4f} LPT ({starting_pending_stake_value:.2f} {currency})"
    )
    print(
        f"Starting pending fees: {starting_pending_fees:.4f} ETH ({starting_pending_fees_value:.2f} {currency})"
    )
    print(
        f"Ending pending stake: {ending_pending_stake:.4f} LPT ({ending_pending_stake_value:.2f} {currency})"
    )
    print(
        f"Ending pending fees: {ending_pending_fees:.4f} ETH ({ending_pending_fees_value:.2f} {currency})"
    )

    print("\nProcessing delegator balances over rounds...")
    balance_data = process_delegator_balances_over_rounds(
        delegator, rounds, currency, starting_pending_stake, starting_pending_fees
    )
    reward_data = balance_data[balance_data["transaction type"] == "pending rewards"]
    fee_data = balance_data[balance_data["transaction type"] == "pending fees"]

    bond_data = fetch_and_process_events(
        address=delegator,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        currency=currency,
        fetch_func=fetch_bond_events,
        process_func=process_bond_events,
        event_name="delegator bond events",
    )

    unbond_data = fetch_and_process_events(
        address=delegator,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        currency=currency,
        fetch_func=fetch_unbond_events,
        process_func=process_unbond_events,
        event_name="delegator unbond events",
    )

    transfer_bond_data = fetch_and_process_events(
        address=delegator,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        currency=currency,
        fetch_func=fetch_transfer_bond_events,
        process_func=lambda events, currency: process_transfer_bond_events(
            transfer_bond_events=events, currency=currency, delegator=delegator
        ),
        event_name="delegator transfer bond events",
    )

    print("\nFetching wallet transactions...")
    transactions_df = fetch_all_transactions(
        address=delegator,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    wallet_transfers = retrieve_token_and_eth_transfers(
        transactions_df=transactions_df, wallet_address=delegator, currency=currency
    )
    all_dataframes = [
        reward_data,
        fee_data,
        bond_data,
        unbond_data,
        transfer_bond_data,
        wallet_transfers,
    ]
    non_empty_dataframes = [df for df in all_dataframes if not df.empty]
    if not non_empty_dataframes:
        print("\033[93mNo income data found, exiting.\033[0m")
        sys.exit(0)

    print("\nCombining all data...")
    combined_df = pd.concat(non_empty_dataframes, ignore_index=True).sort_values(
        by="timestamp"
    )

    print("Adding cumulative balances...")
    combined_df = add_cumulative_balances(
        combined_df=combined_df,
        starting_eth_balance=starting_eth_balance,
        starting_lpt_balance=starting_lpt_balance,
    )

    print(f"\nOverview ({start_time} - {end_time}):")
    overview_table = generate_overview_table(
        delegator=delegator,
        start_time=start_time,
        end_time=end_time,
        reward_data=reward_data,
        fee_data=fee_data,
        currency=currency,
        starting_eth_balance=starting_eth_balance,
        starting_eth_value=starting_eth_value,
        starting_lpt_balance=starting_lpt_balance,
        starting_lpt_value=starting_lpt_value,
        end_eth_balance=end_eth_balance,
        end_eth_value=end_eth_value,
        end_lpt_balance=end_lpt_balance,
        end_lpt_value=end_lpt_value,
        starting_pending_stake=starting_pending_stake,
        starting_pending_fees=starting_pending_fees,
        start_lpt_price=start_lpt_price,
        start_eth_price=start_eth_price,
        end_lpt_price=end_lpt_price,
        end_eth_price=end_eth_price,
    )
    print(tabulate(overview_table, headers=["Metric", "Value"], tablefmt="grid"))

    print("\nExporting data to Excel...")
    combined_df = combined_df[get_csv_column_order(currency)]
    overview_df = pd.DataFrame(overview_table, columns=["Metric", "Value"])
    with ExcelWriter("delegator_income.xlsx") as writer:
        overview_df.to_excel(writer, sheet_name="overview", index=False)
        reward_transactions = combined_df[combined_df["currency"] == "LPT"]
        reward_transactions.to_excel(writer, sheet_name="LPT transactions", index=False)
        fee_transactions = combined_df[combined_df["currency"] == "ETH"]
        fee_transactions.to_excel(writer, sheet_name="ETH transactions", index=False)
        combined_df.to_excel(writer, sheet_name="all transactions", index=False)

    print("Excel export completed.")
