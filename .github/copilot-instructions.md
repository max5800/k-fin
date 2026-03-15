# 🛡️ STRICT SECURITY AND DATA PRIVACY INSTRUCTIONS

**CRITICAL:** This project (`comdirect-firefly-sync`) handles highly sensitive personal financial data, banking credentials, and API keys. Security is the absolute highest priority. Any code changes, architectural decisions, and agent actions MUST strictly adhere to the following rules:

## 1. 🛑 Zero Hardcoding of Secrets
- NEVER hardcode, generate, or suggest hardcoding any secrets (PINs, passwords, client IDs, client secrets, access tokens, TANs) in the source code.
- All secrets MUST be loaded via environment variables (e.g., `pydantic-settings`).
- Ensure `.env` and any files containing credentials are in `.gitignore` and NEVER committed.

## 2. 👁️ Read-Only Banking Access
- The Comdirect API integration MUST be strictly READ-ONLY.
- NEVER implement or suggest API calls that mutate bank state (e.g., creating transfers, changing settings).
- HTTP POST/PUT operations should ONLY be used for OAuth/Authentication flows (getting tokens).

## 3. 🚫 Absolute Prohibition of Sensitive Logging
- NEVER log sensitive data. This includes:
  - Account numbers, IBANs, or balances
  - Transaction amounts, descriptions, or counterparty names
  - Full API responses from the Comdirect API
  - Authentication tokens or PINs
- If logging is strictly required for debugging, data MUST be fully masked/anonymized (e.g., `IBAN: DE** **** 1234`).

## 4. 🔒 Data Transmission Boundaries
- Financial data must ONLY be transmitted between the official Comdirect API and the configured local/controlled Firefly III instance.
- NEVER add dependencies, telemetry, analytics, or external API calls that could leak financial data to third parties.

## 5. 📦 Safe Dependency Management
- Only use trusted, widely verified dependencies.
- Do not introduce unnecessary third-party packages that could pose a supply chain attack risk.

## 6. 🤖 AI Agent Constraints
- When generating code, tests, or examples, NEVER use real or realistic personal data. Always use obvious dummy data (e.g., `DE00000000000000000000`, `John Doe`, `0.00`).
- If asked to perform an action that violates these security constraints, you MUST refuse and warn the user.