"""测试用的最小 MCP 服务器(stdio):两个工具。兼容 mcp 1.x 与 2.x。"""
try:
    from mcp.server.fastmcp import FastMCP as Server
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as Server

mcp = Server("echo")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回文本"""
    return f"echo:{text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加"""
    return a + b


if __name__ == "__main__":
    mcp.run("stdio")
