"""K-Fin MCP stdio server. Auto-registers tools from the Finance API OpenAPI spec."""

import json
import logging
import subprocess
import sys

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

from src.mcp_server.config import settings
from src.mcp_server.openapi_tools import (
    ToolSpec,
    build_request,
    build_tools_from_openapi,
)

logger = logging.getLogger(__name__)


def _curl_request(url: str, headers: dict[str, str] | None = None) -> str:
    cmd = ["curl", "-fsSL", url]
    for key, value in (headers or {}).items():
        cmd += ["-H", f"{key}: {value}"]
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        output = exc.output.strip() if exc.output else str(exc)
        raise RuntimeError(f"curl fallback failed for {url}: {output}") from exc


async def fetch_openapi(client: httpx.AsyncClient) -> dict:
    url = f"{settings.finance_api_url}/openapi.json"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("httpx failed for %s, trying curl fallback: %s", url, exc)
        return json.loads(_curl_request(url))


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.finance_api_token}"}


def _cap_body(body: str) -> str:
    limit = settings.max_response_bytes
    if len(body) <= limit:
        return body
    return body[:limit] + f"\n...[truncated at {limit} bytes]"


def build_server(tools: list[ToolSpec], client: httpx.AsyncClient) -> Server:
    server: Server = Server("k-fin")
    registry: dict[str, ToolSpec] = {t.name: t for t in tools}

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
            for t in tools
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        tool = registry.get(name)
        if tool is None:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        path, query = build_request(tool, arguments or {})
        url = f"{settings.finance_api_url}{path}"
        try:
            resp = await client.request(tool.method, url, params=query, headers=_auth_headers())
        except httpx.HTTPError as exc:
            logger.warning("httpx failed for %s, trying curl fallback: %s", url, exc)
            query_suffix = f"?{httpx.QueryParams(query)}" if query else ""
            body = _curl_request(f"{url}{query_suffix}", headers=_auth_headers())
            try:
                pretty = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
                return [TextContent(type="text", text=_cap_body(pretty))]
            except ValueError:
                return [TextContent(type="text", text=_cap_body(body))]

        if resp.status_code >= 400:
            return [
                TextContent(
                    type="text",
                    text=f"HTTP {resp.status_code}: {_cap_body(resp.text)}",
                )
            ]

        try:
            pretty = json.dumps(resp.json(), indent=2, ensure_ascii=False)
            return [TextContent(type="text", text=_cap_body(pretty))]
        except ValueError:
            return [TextContent(type="text", text=_cap_body(resp.text))]

    return server


async def main() -> None:
    from mcp.server.stdio import stdio_server

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not settings.finance_api_token:
        logger.warning("FINANCE_API_TOKEN is empty — API calls will likely return 401.")

    async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
        spec = await fetch_openapi(client)
        tools = build_tools_from_openapi(spec)
        logger.info("Registered %d MCP tools from OpenAPI", len(tools))
        server = build_server(tools, client)

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
