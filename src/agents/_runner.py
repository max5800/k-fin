"""Helper to invoke pydantic-ai agents from any execution context.

pydantic-ai's `Agent.run_sync()` calls `asyncio.run()` internally, which
raises `RuntimeError: this event loop is already running` whenever it is
invoked from a thread that has (or had) an active event loop — e.g. a
FastAPI async endpoint, a starlette BackgroundTask worker thread, or the
main thread of a process that previously ran one.

We sidestep that by running the agent's async coroutine on a dedicated
thread with a brand-new event loop. The thread exits as soon as the
coroutine completes; no pool, no leaked loops.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


def run_in_fresh_loop(coro: Awaitable[T]) -> T:
    """Run an async coroutine on a fresh event loop in a dedicated thread."""
    box: dict = {}

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # propagate to caller thread
            box["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()

    if "error" in box:
        raise box["error"]
    return box["value"]
