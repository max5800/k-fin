"""Application version helpers."""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "unknown"
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) and version else "unknown"


def backend_version() -> str:
    """Return the deployed backend version.

    Production deployments can inject the image tag via
    ``K_FIN_BACKEND_VERSION``. Local/dev processes fall back to
    ``K_FIN_VERSION`` and then to the static package version in
    ``pyproject.toml``.
    """
    for key in ("K_FIN_BACKEND_VERSION", "K_FIN_VERSION"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return _pyproject_version()
