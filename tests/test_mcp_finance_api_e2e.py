"""E2E guard for the M8 MCP path.

This does not require a live OpenClaw gateway. It verifies the same critical
chain locally: OpenAPI -> MCP tool descriptor -> Finance API request -> Postgres
write. The only write tool currently allowed is budget upsert.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.models import Budget, Category, TypeEnum
from src.mcp_server.openapi_tools import build_request, build_tools_from_openapi

AUTH = {"Authorization": "Bearer test-secret"}


def test_mcp_budget_write_tool_reaches_api_and_postgres(db_engine):
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    with Session(db_engine) as session:
        session.add(Category(id="groceries", name="Lebensmittel", type=TypeEnum.VARIABEL))
        session.commit()

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": db_url}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)

        spec = client.get("/openapi.json").json()
        tools = build_tools_from_openapi(spec, include_write_tools=True)
        tool = next(
            t
            for t in tools
            if t.name == "upsert_budget_api_v1_categories_budgets__category_id__put"
        )
        path, query, body = build_request(
            tool,
            {
                "category_id": "groceries",
                "monthly_limit": "250.00",
                "currency": "EUR",
            },
        )

        response = client.request(tool.method, path, params=query, json=body, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["monthly_limit"] == "250.00"

        with Session(db_engine) as session:
            budget = session.execute(select(Budget)).scalar_one()
            assert budget.category_id == "groceries"
            assert budget.monthly_limit == Decimal("250.00")
            assert budget.currency == "EUR"
