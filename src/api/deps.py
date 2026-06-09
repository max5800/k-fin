"""Shared FastAPI dependencies for the Finance API."""

import hmac
from dataclasses import dataclass
from typing import Annotated, Union

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import (
    get_db,  # canonical session factory — override in tests via dependency_overrides
)
from src.core.db.models import User

_bearer = HTTPBearer(auto_error=False)


# Keep the legacy _get_engine accessor so app.py lifespan can still import it.
def _get_engine():
    from src.core.db import get_engine

    return get_engine()


Db = Annotated[Session, Depends(get_db)]


def _require_credentials(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> HTTPAuthorizationCredentials:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials


# ---------------------------------------------------------------------------
# Service principal — represents MCP / Scheduler using the static API_TOKEN
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServicePrincipal:
    """Represents a non-human caller authenticated via the static API_TOKEN."""

    identity: str = "service"


# ---------------------------------------------------------------------------
# current_principal — accepts EITHER a valid JWT (browser) OR API_TOKEN (services)
# ---------------------------------------------------------------------------


def _get_current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_require_credentials)],
    db: Annotated[Session, Depends(get_db)],
) -> Union[User, ServicePrincipal]:
    """Return the authenticated principal or raise 401.

    Resolution order:
    1. Valid JWT → returns User (browser sessions)
    2. Static API_TOKEN match → returns ServicePrincipal (MCP, Scheduler)
    """
    token = credentials.credentials

    # Try JWT first (browser sessions)
    if settings.jwt_secret:
        try:
            from src.api.auth.jwt import decode_token
            from src.api.auth.service import get_user_by_id

            payload = decode_token(token)
            user = get_user_by_id(db, payload["sub"])
            if user is not None and user.is_active:
                return user
        except HTTPException:
            pass  # Fall through to API token check

    # Fall back to static API_TOKEN (service identity)
    if settings.api_token and hmac.compare_digest(token, settings.api_token):
        return ServicePrincipal()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _get_current_user(
    principal: Annotated[
        Union[User, ServicePrincipal], Depends(_get_current_principal)
    ],
) -> User:
    """Return the authenticated User, or raise 403 if caller is a service."""
    if not isinstance(principal, User):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires user authentication, not a service token",
        )
    return principal


# ---------------------------------------------------------------------------
# Legacy alias kept for backward compat — all existing routers use Auth = Depends(require_token)
# ---------------------------------------------------------------------------


def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_require_credentials)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Accept either a valid JWT (browser) or the static API_TOKEN (services)."""
    token = credentials.credentials

    if settings.jwt_secret:
        try:
            from src.api.auth.jwt import decode_token
            from src.api.auth.service import get_user_by_id

            payload = decode_token(token)
            user = get_user_by_id(db, payload["sub"])
            if user is not None and user.is_active:
                return
        except HTTPException:
            pass  # Fall through to API token check

    if settings.api_token and hmac.compare_digest(token, settings.api_token):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing token",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# Exported dependency aliases
# ---------------------------------------------------------------------------

Auth = Depends(_get_current_principal)
CurrentPrincipal = Annotated[
    Union[User, ServicePrincipal], Depends(_get_current_principal)
]
CurrentUser = Annotated[User, Depends(_get_current_user)]
