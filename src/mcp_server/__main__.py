"""Entry point: ``python -m src.mcp_server`` or ``k-fin-mcp`` starts the stdio server."""

import asyncio

from src.mcp_server.server import main


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
