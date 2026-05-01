"""Local conftest for tests/agents/.

The agents/orchestrator/categorization module chain captures
`src.core.config.settings` at import time. `tests/auth/test_jwt.py`
later reloads `src.core.config`, replacing that singleton — modules
that already imported the old reference are now stale.

These tests legitimately exercise the agent code path, so they trigger
that early import. To keep them from polluting downstream tests
(notably `tests/test_agent_orchestrator.py::TestSearchWebTool`, which
patches the post-reload singleton), we evict the agent modules from
`sys.modules` after each test. The next downstream import gets a fresh
module that captures the current `cfg_mod.settings`.
"""

from __future__ import annotations

import sys

import pytest


_AGENT_MODULES = (
    "src.agents.orchestrator",
    "src.agents.categorization",
    "src.agents._anthropic",
    "src.agents.reaper",
    "src.agents.gather",
    "src.agents.anomaly",
    "src.agents.weekly_analysis",
    "src.agents.monthly_analysis",
    "src.agents.synthesizer",
)


@pytest.fixture(autouse=True)
def _evict_agent_modules_after_test():
    """After each test, drop captured agent modules from sys.modules.

    This keeps a captured-old-`settings` reference from leaking into
    downstream tests that patch the post-reload singleton.
    """
    yield
    for name in _AGENT_MODULES:
        sys.modules.pop(name, None)
