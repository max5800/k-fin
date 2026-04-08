Run the security-reviewer agent on the entire codebase. Focus on:

1. Scan all Python files for hardcoded secrets, tokens, or credentials
2. Check logging statements for sensitive data (IBANs, balances, tokens)
3. Verify .gitignore covers all secret files
4. Check API endpoints for auth and path traversal
5. Review Docker configuration for credential isolation
6. Check dependencies for known vulnerabilities

Report all findings with severity, file location, and fix suggestion.
