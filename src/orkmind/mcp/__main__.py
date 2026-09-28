"""Entry point for running OrkMind MCP server: python -m orkmind.mcp"""

import asyncio

from orkmind.mcp.server import run_server

if __name__ == "__main__":
    asyncio.run(run_server())
