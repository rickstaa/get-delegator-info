"""Retrieve the delegator LPT and ETH balance on Arbitrum on a given timestamp."""

import sys
from web3 import Web3
from tabulate import tabulate
import pandas as pd
from pandas import ExcelWriter

from get_orch_income import (
    fetch_crypto_price,
    human_to_unix_time,
    BONDING_MANAGER_CONTRACT,
    ROUNDS_MANAGER_CONTRACT,
    LPT_TOKEN_CONTRACT,
    ARB_CLIENT,
)


def fetch_eth_balance(wallet_address: str, block_number: int) -> float:
    """Fetch the ETH balance of a wallet at a specific block.

    Args:
        wallet_address: The wallet address to check.
        block_number: The block number to check the balance at.

    Returns:
        The ETH balance in the wallet at the specified block.
    """
    balance_wei = ARB_CLIENT.eth.get_balance(
        wallet_address, block_identifier=block_number
    )
    return balance_wei / 10**18


def fetch_lpt_balance(wallet_address: str, block_number: int) -> float:
    """Fetch the unbonded LPT balance of a wallet at a specific block.

    Args:
        wallet_address: The wallet address to check.
        block_number: The block number to check the balance at.

    Returns:
        The unbonded LPT balance in the wallet at the specified block.
    """
    return (
        LPT_TOKEN_CONTRACT.functions.balanceOf(wallet_address).call(
            block_identifier=block_number
        )
        / 10**18
    )


def fetch_pending_fees(wallet_address: str, round_number: int) -> float:
    """Fetch the pending fees for a delegator at a specific round.

    Args:
        wallet_address: The wallet address to check.
        round_number: The round number to check the pending fees at.

    Returns:
        The pending fees in ETH for the delegator at the specified round.
    """
    return (
        BONDING_MANAGER_CONTRACT.functions.pendingFees(
            wallet_address, round_number
        ).call()
        / 10**18
    )


def fetch_pending_rewards(wallet_address: str, round_number: int) -> float:
    """Fetch the pending rewards for a delegator at a specific round.

    Args:
        wallet_address: The wallet address to check.
        round_number: The round number to check the pending rewards at.

    Returns:
        The pending rewards in LPT for the delegator at the specified round.
    """
    return (
        BONDING_MANAGER_CONTRACT.functions.pendingStake(
            wallet_address, round_number
        ).call()
        / 10**18
    )


def fetch_delegator_balances(
    wallet_address: str, timestamp: int, currency="EUR"
) -> dict:
    """Generate a balance report for a delegator wallet.

    Args:
        wallet_address: The wallet address to check.
        timestamp: The timestamp to check the balances at.
        currency: The currency for the report (default is EUR).

    Returns:
        A dictionary containing the balances and their values.
    """
    block_number = ARB_CLIENT.eth.get_block_number()
    round_number = ROUNDS_MANAGER_CONTRACT.functions.currentRound().call()

    # Fetch balances.
    eth_balance = fetch_eth_balance(wallet_address, block_number)
    lpt_unbonded_balance = fetch_lpt_balance(wallet_address, block_number)
    eth_unclaimed_fees = fetch_pending_fees(wallet_address, round_number)
    lpt_bonded_balance = fetch_pending_rewards(wallet_address, round_number)

    # Fetch prices.
    eth_price = fetch_crypto_price(
        crypto_symbol="ETH", target_currency=currency, unix_timestamp=timestamp
    )
    lpt_price = fetch_crypto_price(
        crypto_symbol="LPT", target_currency=currency, unix_timestamp=timestamp
    )

    # Calculate values.
    eth_value = eth_balance * eth_price
    lpt_unbonded_value = lpt_unbonded_balance * lpt_price
    eth_unclaimed_fees_value = eth_unclaimed_fees * eth_price
    lpt_bonded_value = lpt_bonded_balance * lpt_price

    # Calculate total wallet value.
    total_wallet_value = (
        eth_value + lpt_unbonded_value + eth_unclaimed_fees_value + lpt_bonded_value
    )

    return {
        "eth_balance": eth_balance,
        "eth_value": eth_value,
        "lpt_unbonded_balance": lpt_unbonded_balance,
        "lpt_unbonded_value": lpt_unbonded_value,
        "eth_unclaimed_fees": eth_unclaimed_fees,
        "eth_unclaimed_fees_value": eth_unclaimed_fees_value,
        "lpt_bonded_balance": lpt_bonded_balance,
        "lpt_bonded_value": lpt_bonded_value,
        "total_wallet_value": total_wallet_value,
    }


def create_balance_table(
    date_time: str, wallet_address: str, balances: dict, currency: str
) -> list:
    """Create a table for the delegator balance report.

    Args:
        date_time: The date and time of the report.
        wallet_address: The wallet address to include in the report.
        balances: A dictionary containing the balances and their values.
        currency: The currency for the report.

    Returns:
        A list of lists representing the table rows.
    """
    return [
        [
            "Wallet Address",
            "",
            wallet_address,
        ],
        [
            "Timestamp",
            "",
            date_time,
        ],
        [
            "ETH",
            f"{balances['eth_balance']:.4f} ETH",
            f"{balances['eth_value']:.2f} {currency}",
        ],
        [
            "LPT (unbonded)",
            f"{balances['lpt_unbonded_balance']:.4f} LPT",
            f"{balances['lpt_unbonded_value']:.2f} {currency}",
        ],
        [
            "LPT (bonded)",
            f"{balances['lpt_bonded_balance']:.4f} LPT",
            f"{balances['lpt_bonded_value']:.2f} {currency}",
        ],
        [
            "ETH (unclaimed)",
            f"{balances['eth_unclaimed_fees']:.4f} ETH",
            f"{balances['eth_unclaimed_fees_value']:.2f} {currency}",
        ],
        [
            "Total Wallet Value",
            "-",
            f"{balances['total_wallet_value']:.2f} {currency}",
        ],
    ]


if __name__ == "__main__":
    print("== Delegator Arbitrum LPT/ETH Balance Report ==")
    wallet_address = input("Enter delegator wallet address: ").strip().lower()
    checksum_address = Web3.to_checksum_address(wallet_address)
    date_time = input("Enter date and time (YYYY-MM-DD HH:MM:SS): ").strip()
    timestamp = human_to_unix_time(human_time=date_time)
    currency = input("Enter currency (default: EUR): ").strip().upper() or "EUR"
    if not wallet_address:
        print("Wallet address is required.")
        sys.exit(1)

    print("Generating balance report...")
    balances = fetch_delegator_balances(
        wallet_address=checksum_address, timestamp=timestamp, currency=currency
    )
    table = create_balance_table(
        date_time=date_time,
        wallet_address=wallet_address,
        balances=balances,
        currency=currency,
    )

    print(
        tabulate(
            table,
            headers=["Metric", "Amount", "Value"],
            tablefmt="grid",
        )
    )

    print("\nExporting data to Excel...")
    excel_filename = "delegator_balance_report.xlsx"
    df = pd.DataFrame(table, columns=["Metric", "Amount", "Value"])
    with ExcelWriter(excel_filename) as writer:
        df.to_excel(writer, sheet_name="delegator balance report", index=False)
    print(f"Export completed: {excel_filename}")
