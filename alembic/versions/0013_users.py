"""add users table with bootstrap seed

Revision ID: 0013_users
Revises: 0012_add_portfolio
Create Date: 2026-04-30
"""

from __future__ import annotations

import os
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0013_users"
down_revision = "0012_add_portfolio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String, nullable=False, unique=True),
        sa.Column("display_name", sa.String, nullable=False, server_default=""),
        sa.Column("password_hash", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("role", sa.String(32), nullable=False, server_default="admin"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # Seed the bootstrap user idempotently from environment variables.
    # These env vars are temporary (M10a only) — removed after ESO/Vault
    # has been wired up and the user table has been bootstrapped in prod.
    email = os.environ.get("BOOTSTRAP_USER_EMAIL", "").strip()
    display_name = os.environ.get("BOOTSTRAP_USER_DISPLAY_NAME", "Max").strip()
    initial_password = os.environ.get("BOOTSTRAP_USER_INITIAL_PASSWORD", "").strip()

    if email and initial_password:
        # Import lazily to avoid circular dependencies at migration time.
        # argon2-cffi must be installed in the migration environment.
        from argon2 import PasswordHasher  # type: ignore[import-untyped]

        ph = PasswordHasher()
        password_hash = ph.hash(initial_password)
        user_id = str(uuid.uuid4())

        op.execute(
            sa.text("""
                INSERT INTO users (id, email, display_name, password_hash, is_active, role)
                VALUES (:id, :email, :display_name, :password_hash, true, 'admin')
                ON CONFLICT (email) DO NOTHING
                """).bindparams(
                id=user_id,
                email=email,
                display_name=display_name,
                password_hash=password_hash,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
