"""JWT issue and verify helpers.

Tokens are HS256, signed with JWT_SECRET from settings.
Claims: iss=k-fin, aud=k-fin-ui, sub=user.id (UUID string), exp=60 min.

Security notes:
- algorithm=none is explicitly rejected by passing algorithms=["HS256"] only.
- aud and iss are verified on decode.
- Empty secret causes a hard startup error rather than silent insecurity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException, status

from src.core.config import settings

_ISSUER = "k-fin"
_AUDIENCE = "k-fin-ui"


def _secret() -> str:
    if not settings.jwt_secret:
        raise RuntimeError(
            "JWT_SECRET is not configured. "
            "Set it via environment variable before starting the API."
        )
    return settings.jwt_secret


def issue_token(user_id: str) -> str:
    """Issue a signed JWT for the given user UUID."""
    now = datetime.now(UTC)
    payload = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT.  Raises HTTP 401 on any failure.

    Explicitly passes algorithms=["HS256"] so PyJWT rejects alg=none
    and any asymmetric algorithm.
    """
    try:
        return jwt.decode(
            token,
            _secret(),
            algorithms=[settings.jwt_algorithm],
            issuer=_ISSUER,
            audience=_AUDIENCE,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
