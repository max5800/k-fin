"""Main sync job — orchestrates Comdirect → Firefly pipeline.

NOTE: The Comdirect API requires interactive TAN confirmation (pushTAN)
for every session. This means the scheduler CANNOT run fully unattended.
The sync job will skip execution unless the client already holds a valid
secondary token (e.g., from a manual authenticate_full() call).

Future options to solve this:
- Token persistence: store secondary token + refresh token after manual auth
- Webhook-triggered flow: user confirms TAN via mobile, webhook resumes sync
- Headless TAN: if Comdirect ever supports non-interactive auth
"""

from src.connector.comdirect_client import ComdirectClient
from src.core.logging import get_logger
from src.importer.firefly_client import FireflyClient
from src.importer.transaction_mapper import map_transaction

logger = get_logger("sync_job")


async def run_sync():
    """Full sync: pull from Comdirect, push to Firefly III.

    Requires the client to be authenticated with a secondary token
    (full 6-step TAN flow). Will skip if auth is not possible.
    """
    logger.info("Starting sync...")
    comdirect = ComdirectClient()
    firefly = FireflyClient()

    # Auth: requires full 6-step flow with TAN confirmation.
    # authenticate_full() blocks for user input — only works interactively.
    auth_ok = await comdirect.authenticate_full()
    if not auth_ok:
        logger.error("Comdirect auth failed — skipping sync")
        return

    # Step 2: Fetch accounts
    try:
        accounts = await comdirect.get_accounts()
        logger.info(f"Found {len(accounts)} Comdirect accounts")
    except Exception as e:
        logger.error(f"Failed to fetch accounts: {e}")
        return

    imported = 0
    skipped = 0

    for account in accounts:
        account_id = account.get("account", {}).get("accountId")
        iban = account.get("account", {}).get("iban")
        if not account_id or not iban:
            continue

        # Find matching Firefly account
        firefly_account = await firefly.find_account_by_iban(iban)
        if not firefly_account:
            logger.warning(
                f"No Firefly account found for IBAN {iban[:8]}*** — skipping"
            )
            continue

        firefly_account_id = firefly_account["id"]

        # Step 3: Fetch transactions
        try:
            transactions = await comdirect.get_transactions(account_id)
        except Exception as e:
            logger.error(f"Failed to fetch transactions for {iban[:8]}***: {e}")
            continue

        for tx in transactions:
            tx_id = tx.get("transactionId", "")
            # Dedup check
            if await firefly.transaction_exists(tx_id):
                skipped += 1
                continue

            payload = map_transaction(tx, firefly_account_id)
            try:
                await firefly.create_transaction(payload)
                imported += 1
            except Exception as e:
                logger.error(f"Failed to import transaction {tx_id}: {e}")

    logger.info(f"Sync complete — imported: {imported}, skipped (dedup): {skipped}")
