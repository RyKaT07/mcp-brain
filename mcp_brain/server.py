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
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8400"))

mcp = FastMCP(
    "mcp-brain",
    instructions="Personal knowledge base and productivity MCP server.",
    host=HOST,
    port=PORT,
)

# Register all tool groups
register_knowledge_tools(mcp, KNOWLEDGE_DIR)
register_inbox_tools(mcp, KNOWLEDGE_DIR)
register_briefing_tools(mcp, KNOWLEDGE_DIR)
register_secrets_tools(mcp, KNOWLEDGE_DIR)


def main():
    """Run the MCP server. Default transport is SSE for remote access."""
    transport = os.getenv("MCP_TRANSPORT", "sse")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
