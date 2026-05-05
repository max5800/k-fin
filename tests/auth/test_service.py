"""Unit tests for auth service — focus on timing-equalisation.

Login must spend roughly the same CPU on a known-email/wrong-password attempt
as on an unknown-email attempt. Otherwise an attacker can probe valid emails
by measuring response latency.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.api.auth import service
from src.api.auth.service import authenticate_user


def test_unknown_email_still_runs_argon2():
    """Unknown email must call verify_password against the dummy hash."""
    db = MagicMock()
    with (
        patch.object(service, "get_user_by_email", return_value=None),
        patch.object(service, "verify_password", return_value=False) as vp,
    ):
        result = authenticate_user(db, "nobody@example.com", "whatever")

    assert result is None
    vp.assert_called_once()
    called_password, called_hash = vp.call_args.args
    assert called_password == "whatever"
    assert called_hash == service._DUMMY_HASH


def test_inactive_user_still_runs_argon2():
    """Inactive user must also exercise argon2 — same timing shape as unknown email."""
    db = MagicMock()
    inactive = MagicMock(is_active=False, password_hash="real-hash-but-unused")
    with (
        patch.object(service, "get_user_by_email", return_value=inactive),
        patch.object(service, "verify_password", return_value=False) as vp,
    ):
        result = authenticate_user(db, "disabled@example.com", "whatever")

    assert result is None
    vp.assert_called_once()
    _, called_hash = vp.call_args.args
    assert called_hash == service._DUMMY_HASH


def test_known_email_wrong_password_runs_argon2_once():
    """Sanity baseline: real lookup hits verify_password exactly once."""
    db = MagicMock()
    user = MagicMock(is_active=True, password_hash="stored-hash")
    with (
        patch.object(service, "get_user_by_email", return_value=user),
        patch.object(service, "verify_password", return_value=False) as vp,
    ):
        result = authenticate_user(db, "max@example.com", "wrong")

    assert result is None
    vp.assert_called_once_with("wrong", "stored-hash")
