"""Bootstrap login endpoint — DEV ONLY.

Issues the static API_TOKEN after a fixed credential check so the
frontend can drive the full UI end-to-end during local development.
This is not real authentication: there is no user table, no hashing,
no session expiry. Hard-disabled when app_env == "production".
"""

import hmac

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.core.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _bootstrap_disabled() -> bool:
    return (
        not settings.bootstrap_login_enabled
        or settings.app_env == "production"
    )


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    if _bootstrap_disabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    if not (settings.bootstrap_email and settings.bootstrap_password and settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bootstrap login is enabled but misconfigured",
        )

    email_ok = hmac.compare_digest(payload.email.lower(), settings.bootstrap_email.lower())
    password_ok = hmac.compare_digest(payload.password, settings.bootstrap_password)
    if not (email_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    return LoginResponse(access_token=settings.api_token)
