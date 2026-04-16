"""Weekly sync pipeline — documents the end-to-end flow.

The full weekly sync is triggered via the worker's two-step HTTP flow:

    1. POST /internal/sync/start  → begins OAuth + pushTAN challenge
    2. (user confirms TAN on phone)
    3. POST /internal/sync/confirm → fetches data, exports, normalizes, runs agents

All steps are implemented in main.py (worker entrypoints). This module is
reserved for future standalone/scheduled invocation if needed.

Pipeline stages (all in sync/confirm):
    fetch → CSV export → JSON export → ingest raw → normalize → agent pipeline
"""
