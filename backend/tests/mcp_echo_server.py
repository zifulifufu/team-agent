"""Minimal stdio MCP server used by the tests: two tools.

Works with both mcp 1.x and 2.x.
"""
try:
    from mcp.server.fastmcp import FastMCP as Server
except ModuleNotFoundError:  # mcp 2.x moved the fast server
    from mcp.server.mcpserver import MCPServer as Server

mcp = Server("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"echo:{text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    mcp.run("stdio")
