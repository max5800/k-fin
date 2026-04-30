"""User auth service: lookup and authenticate users."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.api.auth.passwords import hash_password, needs_rehash, verify_password
from src.core.db.models import User


def get_user_by_id(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email.lower()).first()


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Return the User if credentials are valid and account is active, else None."""
    user = get_user_by_email(db, email)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None

    # Opportunistic rehash if Argon2 parameters have been upgraded.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user
