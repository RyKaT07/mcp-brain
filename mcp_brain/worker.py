"""
mcp-brain single-user sandboxed worker.

Stripped-down MCP server designed to run inside a bwrap sandbox.  Key
differences from the main server (server.py):

- No auth middleware.  The sandbox boundary IS the security boundary.
  The worker only sees its own /data/knowledge and /data/state; there is
  nothing else to protect.
- Listens on a Unix domain socket (path from --socket CLI arg).
- Knowledge dir is fixed at /data/knowledge (bwrap-mounted).
- State dir is fixed at /data/state (search index + relationship graph).
- Only knowledge / maintain / briefing tools are registered.  Integration
  tools (Todoist, Trello, etc.) are NOT included — this worker is for
  per-user personal memory only.
- No OAuth, no admin routes, no CSP middleware.

Usage (from bwrap command line):
    python3 -m mcp_brain.worker --socket /run/mcp-brain/sockets/{user_id}.sock
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_brain.search import SearchIndex
from mcp_brain.graph import RelationshipGraph
from mcp_brain.tools.knowledge import register_knowledge_tools
from mcp_brain.tools.maintain import register_maintain_tools
from mcp_brain.tools.meta import register_meta_tools
from mcp_brain.tools.briefing import register_briefing_tools
from mcp_brain.tools.search import register_search_tools
from mcp_brain.tools.graph import register_graph_tools
from mcp_brain.tools.wake import register_wake_tools

logger = logging.getLogger(__name__)

# Fixed paths inside the bwrap sandbox.
KNOWLEDGE_DIR = Path(os.getenv("MCP_KNOWLEDGE_DIR", "/data/knowledge"))
STATE_DIR = Path(os.getenv("MCP_STATE_DIR", "/data/state"))


def _build_worker_mcp(socket_path: Path) -> FastMCP:
    """Build and configure the FastMCP instance for the sandboxed worker.

    No auth is wired in — the bwrap sandbox enforces isolation.
    """
    mcp = FastMCP(
        "mcp-brain-worker",
        # Unix socket transport will be configured at run time;
        # host/port are unused for socket mode but required by FastMCP.
        host="127.0.0.1",
        port=0,
    )

    # Ensure state directory exists.
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    search_index = SearchIndex()
    search_index.build(KNOWLEDGE_DIR)

    rel_graph = RelationshipGraph()
    rel_graph.build(KNOWLEDGE_DIR)

    register_knowledge_tools(mcp, KNOWLEDGE_DIR, search_index=search_index, rel_graph=rel_graph)
    register_maintain_tools(mcp, KNOWLEDGE_DIR)
    register_meta_tools(mcp, KNOWLEDGE_DIR)
    register_briefing_tools(mcp, KNOWLEDGE_DIR)
    register_search_tools(mcp, KNOWLEDGE_DIR, search_index)
    register_graph_tools(mcp, KNOWLEDGE_DIR, rel_graph)
    # brain_wake registered last so its inventory snapshot is complete.
    register_wake_tools(mcp, KNOWLEDGE_DIR)

    return mcp


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="mcp-brain sandboxed worker")
    parser.add_argument(
        "--socket",
        required=True,
        type=Path,
        help="Path to the Unix domain socket to listen on",
    )
    args = parser.parse_args()

    socket_path: Path = args.socket

    mcp = _build_worker_mcp(socket_path)

    logger.info("worker: starting on socket %s", socket_path)
    # FastMCP supports Unix socket transport via the `transport="unix"` mode
    # with the socket path passed as the host.  If the MCP library version
    # in use does not yet support unix sockets natively, fall back to stdio
    # so tests still work — the process manager detects socket absence and
    # raises an error before any real traffic flows.
    try:
        mcp.run(transport="unix", socket_path=str(socket_path))  # type: ignore[call-arg]
    except TypeError:
        # Fallback for older FastMCP versions that do not accept socket_path.
        logger.warning("worker: FastMCP does not support unix transport — falling back to stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
