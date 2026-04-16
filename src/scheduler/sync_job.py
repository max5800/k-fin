"""Main sync job — orchestrates Comdirect data pull and normalization.

NOTE: The Comdirect API requires interactive TAN confirmation (pushTAN)
for every session. Auth is handled via the two-step flow in the worker
endpoints (POST /internal/sync/start → confirm TAN → POST /internal/sync/confirm).

This module provides run_sync() which accepts a pre-authenticated client.
"""

from src.connector.comdirect_client import ComdirectClient
from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger("sync_job")


async def run_sync(comdirect: ComdirectClient | None = None):
    """Full sync: pull from Comdirect and store/normalize data.

    Accepts an already-authenticated ComdirectClient. If none is provided,
    logs an error and returns (auth must be done via the two-step flow).
    """
    logger.info("Starting sync...")
    if comdirect is None or not comdirect.is_authenticated:
        logger.error("No authenticated client provided — skipping sync")
        return

    # Step 1: Fetch accounts
    try:
        accounts = await comdirect.get_accounts()
        logger.info(f"Found {len(accounts)} Comdirect accounts")
    except Exception as e:
        logger.error(f"Failed to fetch accounts: {e}")
        return

    for account in accounts:
        account_id = account.get("account", {}).get("accountId")
        if not account_id:
            continue

        # Fetch transactions
        try:
            transactions = await comdirect.get_transactions(
                account_id,
                paging_count=settings.account_transaction_limit,
                min_booking_date=settings.account_transaction_min_booking_date,
            )
            logger.info(
                f"Fetched {len(transactions)} transactions for account {account_id}"
            )
        except Exception as e:
            logger.error(f"Failed to fetch transactions for account {account_id}: {e}")
            continue

    logger.info("Sync complete")
