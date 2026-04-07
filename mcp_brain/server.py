"""
mcp-brain: Personal MCP server for persistent AI memory and integrations.
"""

import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

from mcp_brain.tools.knowledge import register_knowledge_tools
from mcp_brain.tools.inbox import register_inbox_tools
from mcp_brain.tools.briefing import register_briefing_tools
from mcp_brain.tools.secrets_schema import register_secrets_tools

KNOWLEDGE_DIR = Path(os.getenv("MCP_KNOWLEDGE_DIR", "./knowledge"))

mcp = FastMCP(
    "mcp-brain",
    version="0.1.0",
    description="Personal knowledge base and productivity MCP server",
)

# Register all tool groups
register_knowledge_tools(mcp, KNOWLEDGE_DIR)
register_inbox_tools(mcp, KNOWLEDGE_DIR)
register_briefing_tools(mcp, KNOWLEDGE_DIR)
register_secrets_tools(mcp, KNOWLEDGE_DIR)


def main():
    """Run the MCP server with SSE transport for remote access."""
    transport = os.getenv("MCP_TRANSPORT", "sse")
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8400"))

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="sse", host=host, port=port)


if __name__ == "__main__":
    main()
