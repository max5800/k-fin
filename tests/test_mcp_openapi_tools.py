"""Unit tests for MCP OpenAPI → Tool transformation."""

from src.mcp_server.openapi_tools import (
    build_request,
    build_tools_from_openapi,
)


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
    path, query = build_request(tool, {"transaction_id": 42, "include_tags": True})
    assert path == "/api/v1/transactions/42"
    assert query == {"include_tags": True}


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
    path, query = build_request(tool, {})
    assert path == "/api/v1/transactions"
    assert query == {}


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
