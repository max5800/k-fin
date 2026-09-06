---
name: k-fin-finance-api
description: Read balances, transactions, portfolio positions and financial summaries from the configured k-fin MCP or Finance API.
---

# k-fin Finance API

Use the available k-fin MCP integration for the requested financial data. Its tool catalog and input schemas are generated from the backend's OpenAPI document; discover the relevant read operation instead of guessing an old endpoint or fixed tool name. Select the requested account/source and period, and paginate when needed.

If MCP is unavailable and direct API access is already configured and authorized, use that deployment's `FINANCE_API_URL`, its current OpenAPI schema and the approved Bearer-auth integration. Do not read arbitrary credential files or put tokens in URLs, prompts or output. Connection details are in [references/api.md](references/api.md); load them only for access diagnosis or direct API use.

Keep read-only analysis read-only. The backend may expose supported mutations, but tool availability does not authorize imports, budget changes, categorization, reconciliation, exports, bank operations or credential changes. Do not enable write tools merely to answer a data question. Bank access remains read-only.

Bind conclusions to the source period and coverage. Do not count card/PayPal settlement transfers again as merchant consumption; distinguish refunds, investments and internal transfers. State gaps or stale evidence without starting maintenance. Mask private identifiers and return only the financial detail needed for the requested answer.

Return results to the requester. Saving a report or sending a separate notification follows the actual request or existing scheduled-job authority; this skill adds neither effect automatically. Historical CSVs supplied by the user can still be analyzed using the format notes in the reference.
