# Current k-fin access

Prefer the registered k-fin MCP integration. The server loads `/openapi.json` and derives available tool schemas from the deployed API. Schema and deployment configuration take precedence over examples from old export services.

For an already configured direct integration, the MCP settings are `FINANCE_API_URL` and `FINANCE_API_TOKEN`; authenticated requests use `Authorization: Bearer …`, never a query token. The source default URL is for local development, not proof of the operator's actual target. Use the configured endpoint and approved credential mechanism without exposing values.

`MCP_ENABLE_WRITE_TOOLS` defaults to false. Enabling it changes the available tool surface and requires a separately authorized integration change; it is not a prerequisite for ordinary analysis. The server caps responses, so follow pagination and distinguish truncation from a complete dataset. An access failure warrants a bounded check of the selected integration and relevant configuration, not a fallback to the retired `/exports` service or an automatic credential repair.

## User-supplied historical CSVs

Archived Comdirect CSVs may use semicolon delimiters, UTF-8-sig, `DD.MM.YYYY` dates and German decimal notation (`1.234,56` means `1234.56`). Confirm the actual file's headers and period before parsing. This format guidance does not imply that a CSV-export HTTP service exists or authorize a new bank export.
