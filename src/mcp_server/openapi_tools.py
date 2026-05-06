"""Transform an OpenAPI spec into MCP tool descriptors.

Read-only by default. A tiny `WRITE_ALLOWLIST` opts specific non-GET operations
into the tool registry — keep it minimal and explicit; this server must never
expose arbitrary writes against the Finance API.
"""

from dataclasses import dataclass, field
from typing import Any

SKIP_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}
SKIP_PATH_PREFIXES = ("/internal/",)

# Explicit allowlist for non-GET operations. Anything not listed here is
# rejected even if the caller passes a broader `methods` tuple — this keeps
# accidental writes from sneaking in via OpenAPI changes.
WRITE_ALLOWLIST: set[tuple[str, str]] = {
    ("put", "/api/v1/categories/budgets/{category_id}"),
}


@dataclass
class ToolSpec:
    name: str
    description: str
    method: str
    path_template: str
    parameters: list[dict[str, Any]]
    input_schema: dict[str, Any]
    body_fields: list[str] = field(default_factory=list)


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


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _request_body_schema(
    spec: dict[str, Any], op: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Return (properties, required) for the JSON request body, resolving $ref."""
    request_body = op.get("requestBody") or {}
    content = request_body.get("content") or {}
    json_content = content.get("application/json") or {}
    schema = json_content.get("schema") or {}
    if "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])
    properties = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    return properties, required


def build_tools_from_openapi(
    spec: dict[str, Any],
    methods: tuple[str, ...] = ("get",),
) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            method_l = method.lower()
            allow_via_methods = method_l in methods
            allow_via_writelist = (method_l, path) in WRITE_ALLOWLIST
            if not (allow_via_methods or allow_via_writelist):
                continue
            if not isinstance(op, dict):
                continue
            if _skip_operation(path, op):
                continue
            parameters = op.get("parameters") or []
            input_schema = _input_schema_from_params(parameters)
            body_props, body_required = _request_body_schema(spec, op)
            if body_props:
                properties = input_schema.setdefault("properties", {})
                for name, schema in body_props.items():
                    properties[name] = schema
            if body_required:
                required = input_schema.setdefault("required", [])
                for name in body_required:
                    if name not in required:
                        required.append(name)
            tools.append(
                ToolSpec(
                    name=_tool_name(path, method, op),
                    description=_tool_description(op),
                    method=method.upper(),
                    path_template=path,
                    parameters=parameters,
                    input_schema=input_schema,
                    body_fields=list(body_props.keys()),
                )
            )
    return tools


def build_request(
    tool: ToolSpec, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Split arguments into a substituted path, query dict, and JSON body.

    `body` is `None` for tools without a request body (e.g. plain GETs); for
    tools with body fields it is a dict containing only those fields actually
    supplied by the caller.
    """
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
    body: dict[str, Any] | None = None
    if tool.body_fields:
        body = {name: arguments[name] for name in tool.body_fields if name in arguments}
    return path, query, body
