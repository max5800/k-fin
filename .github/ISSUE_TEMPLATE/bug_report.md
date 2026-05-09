---
name: Bug report
about: Report a defect in k-fin
title: "bug: <short summary>"
labels: bug
assignees: ''
---

## Steps to reproduce

1. ...
2. ...
3. ...

## Expected behavior

What you expected to happen.

## Actual behavior

What actually happened. Include error messages verbatim.

## Logs

Paste relevant log output inside a fenced block.

> **Redact before pasting.** Strip IBANs, account numbers, balances, transaction
> amounts, access/refresh tokens, session IDs, JWTs, PINs, and any Comdirect
> identifiers. Replace with placeholders like `DE00000000000000000000` or `<redacted>`.

```
<paste redacted log here>
```

## Environment

- k-fin version / commit:
- Deployment: docker-compose / Helm / local `uv run`
- Python version (`python --version`):
- OS:

## Additional context

Anything else that helps diagnose — recent config changes, related issues, etc.
