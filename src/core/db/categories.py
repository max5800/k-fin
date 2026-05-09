"""Well-known category id constants.

Concrete category rows are seeded by Alembic migration 0005. A handful of
ids are referenced from code paths (refund-audit walker, dev seed, agent
prompts, ops scripts) and must stay in sync with the migration. Centralise
them here so a rename only touches one place.
"""

from __future__ import annotations

# Catch-all income bucket used by the categorization agent when a
# positive-amount tx doesn't match any specific income category. The
# refund-audit pipeline walks rows in this bucket to decide whether they
# are real income (Steuerrückzahlung, Cashback, Zinsen) or a misclassified
# refund of a prior expense (Krankenkasse, Splitwise, Retouren).
INCOME_CATCHALL_CATEGORY_ID = "erstattungen"
