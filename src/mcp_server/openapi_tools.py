"""Transform an OpenAPI spec into MCP tool descriptors. Read-only scope."""

from dataclasses import dataclass
from typing import Any

SKIP_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}
SKIP_PATH_PREFIXES = ("/internal/",)


@dataclass
class ToolSpec:
    name: str
    description: str
    method: str
    path_template: str
    parameters: list[dict[str, Any]]
    input_schema: dict[str, Any]


def _skip_operation(path: str, op: dict[str, Any]) -> bool:
    if path in SKIP_PATHS:
        return True
    if any(path.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
        return True
    if "internal" in (op.get("tags") or []):
        return True
    return False


def _tool_name(path: str, method: str, op: dict[str, Any]) -> str:
    op_id = op.get("operationId")
    if op_id:
        return op_id
    clean = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{method.lower()}_{clean}" if clean else method.lower()


def _tool_description(op: dict[str, Any]) -> str:
    return op.get("description") or op.get("summary") or ""


def _input_schema_from_params(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in parameters:
        schema = dict(p.get("schema") or {})
        if "description" not in schema and p.get("description"):
            schema["description"] = p["description"]
        properties[p["name"]] = schema
        if p.get("required"):
            required.append(p["name"])
    obj: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        obj["required"] = required
    return obj


def build_tools_from_openapi(
    spec: dict[str, Any],
    methods: tuple[str, ...] = ("get",),
) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method.lower() not in methods:
                continue
            if not isinstance(op, dict):
                continue
            if _skip_operation(path, op):
                continue
            parameters = op.get("parameters") or []
            tools.append(
                ToolSpec(
                    name=_tool_name(path, method, op),
                    description=_tool_description(op),
                    method=method.upper(),
                    path_template=path,
                    parameters=parameters,
                    input_schema=_input_schema_from_params(parameters),
                )
            )
    return tools


def build_request(
    tool: ToolSpec, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Split arguments into a substituted path and query dict."""
    path = tool.path_template
    query: dict[str, Any] = {}
    for p in tool.parameters:
        name = p["name"]
        if name not in arguments:
            continue
        value = arguments[name]
        where = p.get("in", "query")
        if where == "path":
            path = path.replace("{" + name + "}", str(value))
        elif where == "query":
            query[name] = value
    return path, query
