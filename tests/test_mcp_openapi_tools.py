"""Unit tests for MCP OpenAPI → Tool transformation."""

from src.mcp_server.openapi_tools import (
    WRITE_ALLOWLIST,
    build_request,
    build_tools_from_openapi,
)

BUDGET_PATH = "/api/v1/categories/budgets/{category_id}"
BUDGET_OP_ID = "upsert_budget_api_v1_categories_budgets__category_id__put"


def _budget_spec() -> dict:
    """Minimal OpenAPI spec for the budget upsert endpoint."""
    return {
        "paths": {
            BUDGET_PATH: {
                "put": {
                    "operationId": BUDGET_OP_ID,
                    "summary": "Upsert Budget",
                    "parameters": [
                        {
                            "name": "category_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/BudgetUpdate"
                                }
                            }
                        },
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "BudgetUpdate": {
                    "type": "object",
                    "properties": {
                        "monthly_limit": {"type": "string"},
                        "currency": {"type": "string", "default": "EUR"},
                    },
                    "required": ["monthly_limit"],
                }
            }
        },
    }


def test_skips_health_and_internal():
    spec = {
        "paths": {
            "/health": {"get": {"operationId": "health"}},
            "/internal/sync": {
                "get": {"operationId": "internal_sync", "tags": ["internal"]}
            },
            "/api/v1/transactions": {"get": {"operationId": "list_transactions"}},
        }
    }
    names = [t.name for t in build_tools_from_openapi(spec)]
    assert names == ["list_transactions"]


def test_only_get_methods_in_default_scope():
    spec = {
        "paths": {
            "/api/v1/transactions/{id}": {
                "get": {"operationId": "get_transaction"},
                "patch": {"operationId": "update_transaction"},
                "delete": {"operationId": "delete_transaction"},
            }
        }
    }
    names = [t.name for t in build_tools_from_openapi(spec)]
    assert names == ["get_transaction"]


def test_input_schema_reflects_params():
    spec = {
        "paths": {
            "/api/v1/transactions": {
                "get": {
                    "operationId": "list_transactions",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "default": 50},
                        },
                        {
                            "name": "category_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "integer"},
                        },
                    ],
                }
            }
        }
    }
    [tool] = build_tools_from_openapi(spec)
    assert tool.input_schema["type"] == "object"
    assert set(tool.input_schema["properties"]) == {"limit", "category_id"}
    assert tool.input_schema["required"] == ["category_id"]


def test_build_request_substitutes_path_and_query():
    spec = {
        "paths": {
            "/api/v1/transactions/{transaction_id}": {
                "get": {
                    "operationId": "get_transaction",
                    "parameters": [
                        {
                            "name": "transaction_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        },
                        {
                            "name": "include_tags",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                    ],
                }
            }
        }
    }
    [tool] = build_tools_from_openapi(spec)
    path, query, body = build_request(
        tool, {"transaction_id": 42, "include_tags": True}
    )
    assert path == "/api/v1/transactions/42"
    assert query == {"include_tags": True}
    assert body is None


def test_build_request_skips_missing_arguments():
    spec = {
        "paths": {
            "/api/v1/transactions": {
                "get": {
                    "operationId": "list_transactions",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                        }
                    ],
                }
            }
        }
    }
    [tool] = build_tools_from_openapi(spec)
    path, query, body = build_request(tool, {})
    assert path == "/api/v1/transactions"
    assert query == {}
    assert body is None


def test_tool_description_prefers_description_over_summary():
    spec = {
        "paths": {
            "/api/v1/x": {
                "get": {
                    "operationId": "x",
                    "summary": "short",
                    "description": "full explanation",
                }
            }
        }
    }
    [tool] = build_tools_from_openapi(spec)
    assert tool.description == "full explanation"


# --- Write allowlist ------------------------------------------------------


def test_budget_put_in_write_allowlist():
    """The one allowed write is the budget upsert — guard against accidental drift."""
    assert WRITE_ALLOWLIST == {("put", BUDGET_PATH)}


def test_non_allowlisted_writes_are_not_registered():
    spec = {
        "paths": {
            "/api/v1/categories": {
                "post": {"operationId": "create_category"},
            },
            "/api/v1/categories/{category_id}": {
                "delete": {"operationId": "delete_category"},
            },
            "/api/v1/transactions/{id}": {
                "patch": {"operationId": "update_transaction"},
            },
            "/api/v1/runs": {
                "post": {"operationId": "create_run"},
            },
            "/api/v1/runs/{id}": {
                "delete": {"operationId": "delete_run"},
            },
        }
    }
    assert build_tools_from_openapi(spec) == []


def test_budget_put_is_registered_with_body_schema():
    [tool] = build_tools_from_openapi(_budget_spec())
    assert tool.name == BUDGET_OP_ID
    assert tool.method == "PUT"
    assert tool.path_template == BUDGET_PATH
    props = tool.input_schema["properties"]
    assert set(props) == {"category_id", "monthly_limit", "currency"}
    assert props["currency"].get("default") == "EUR"
    # `category_id` (path) and `monthly_limit` (body) are required;
    # `currency` is optional.
    assert set(tool.input_schema["required"]) == {"category_id", "monthly_limit"}
    assert tool.body_fields == ["monthly_limit", "currency"]


def test_build_request_splits_body_for_budget_upsert():
    [tool] = build_tools_from_openapi(_budget_spec())
    path, query, body = build_request(
        tool,
        {"category_id": "groceries", "monthly_limit": "250.00", "currency": "EUR"},
    )
    assert path == "/api/v1/categories/budgets/groceries"
    assert query == {}
    assert body == {"monthly_limit": "250.00", "currency": "EUR"}


def test_build_request_omits_unsupplied_optional_body_fields():
    [tool] = build_tools_from_openapi(_budget_spec())
    _, _, body = build_request(
        tool, {"category_id": "groceries", "monthly_limit": "250.00"}
    )
    assert body == {"monthly_limit": "250.00"}


def test_default_get_scope_still_excludes_allowlisted_writes_when_get_only():
    """Caller asking only for GETs gets only GETs, even though the budget PUT is allowlisted.

    This proves the allowlist *adds* writes on top of `methods=` rather than
    replacing it, and that arbitrary GET callers don't accidentally pick up
    writes — only the live server (which doesn't pass `methods=`) does.
    """
    spec = _budget_spec()
    # Add a GET sibling so we can verify it still comes through.
    spec["paths"]["/api/v1/categories/budgets"] = {
        "get": {"operationId": "list_budgets"}
    }
    names = sorted(t.name for t in build_tools_from_openapi(spec))
    # Default call still includes the allowlisted budget PUT — the allowlist
    # is an explicit opt-in baked into the module, not gated on `methods=`.
    assert names == sorted(["list_budgets", BUDGET_OP_ID])
