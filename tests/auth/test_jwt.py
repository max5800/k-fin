"""Unit tests for JWT issue/verify."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import jwt
import pytest


JWT_ENV = {
    "JWT_SECRET": "test-super-secret-key-for-unit-tests-only",
    "DATABASE_URL": "postgresql+psycopg://x:x@localhost/x",  # not used in unit tests
    "API_TOKEN": "",
}


@pytest.fixture(autouse=True)
def patch_settings():
    with patch.dict(os.environ, JWT_ENV, clear=False):
        # Force settings re-instantiation for each test
        import importlib
        import src.core.config as cfg_mod
        importlib.reload(cfg_mod)
        import src.api.auth.jwt as jwt_mod
        importlib.reload(jwt_mod)
        yield


def _issue(user_id: str = "test-uuid") -> str:
    from src.api.auth.jwt import issue_token
    return issue_token(user_id)


def test_issue_and_decode_roundtrip():
    from src.api.auth.jwt import decode_token
    token = _issue("abc-123")
    payload = decode_token(token)
    assert payload["sub"] == "abc-123"
    assert payload["iss"] == "k-fin"
    assert payload["aud"] == "k-fin-ui"


def test_alg_none_rejected():
    """A token signed with alg=none must be rejected."""
    from src.api.auth.jwt import decode_token
    from fastapi import HTTPException

    # Craft a "none"-algorithm token manually
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "iss": "k-fin",
        "aud": "k-fin-ui",
        "sub": "evil",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    # jwt.encode with algorithm=none is blocked by PyJWT >= 2.x by default
    # but craft one the low-level way to be sure
    import base64
    import json

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    crafted = (
        b64url(json.dumps(header).encode())
        + "."
        + b64url(json.dumps(payload).encode())
        + "."
    )
    with pytest.raises(HTTPException) as exc:
        decode_token(crafted)
    assert exc.value.status_code == 401


def test_wrong_issuer_rejected():
    from src.api.auth.jwt import decode_token
    from fastapi import HTTPException

    secret = os.environ["JWT_SECRET"]
    bad = jwt.encode(
        {"iss": "evil", "aud": "k-fin-ui", "sub": "x", "exp": int(time.time()) + 3600},
        secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        decode_token(bad)


def test_wrong_audience_rejected():
    from src.api.auth.jwt import decode_token
    from fastapi import HTTPException

    secret = os.environ["JWT_SECRET"]
    bad = jwt.encode(
        {"iss": "k-fin", "aud": "wrong-aud", "sub": "x", "exp": int(time.time()) + 3600},
        secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        decode_token(bad)


def test_expired_token_rejected():
    from src.api.auth.jwt import decode_token
    from fastapi import HTTPException

    secret = os.environ["JWT_SECRET"]
    expired = jwt.encode(
        {
            "iss": "k-fin",
            "aud": "k-fin-ui",
            "sub": "x",
            "exp": int(time.time()) - 10,
            "iat": int(time.time()) - 70,
        },
        secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        decode_token(expired)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()
