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
import contextvars
import threading
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


def run_in_fresh_loop(coro: Awaitable[T]) -> T:
    """Run an async coroutine on a fresh event loop in a dedicated thread.

    The current ContextVar context is copied into the worker thread so
    things like `pydantic_ai.Agent.override()` (which patches the model
    via a ContextVar) take effect on the LLM call. Without this the
    override is lost across the thread boundary and tests fall through
    to the real Anthropic provider.
    """
    box: dict = {}
    ctx = contextvars.copy_context()

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = ctx.run(loop.run_until_complete, coro)
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
