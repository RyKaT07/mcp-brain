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
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# Fixed paths inside the bwrap sandbox.
KNOWLEDGE_DIR = Path(os.getenv("MCP_KNOWLEDGE_DIR", "/data/knowledge"))
STATE_DIR = Path(os.getenv("MCP_STATE_DIR", "/data/state"))


def _build_worker_mcp():
    """Build and configure the FastMCP instance for the sandboxed worker.

    No auth is wired in — the bwrap sandbox enforces isolation.
    """
    from mcp.server.fastmcp import FastMCP

    from mcp_brain.graph import RelationshipGraph
    from mcp_brain.search import SearchIndex
    from mcp_brain.tools import _perms
    from mcp_brain.tools.briefing import register_briefing_tools
    from mcp_brain.tools.graph import register_graph_tools
    from mcp_brain.tools.inbox import register_inbox_tools
    from mcp_brain.tools.knowledge import register_knowledge_tools
    from mcp_brain.tools.maintain import register_maintain_tools
    from mcp_brain.tools.meta import register_meta_tools
    from mcp_brain.tools.search import register_search_tools
    from mcp_brain.tools.wake import register_wake_tools

    mcp = FastMCP("mcp-brain-worker")

    # No auth in worker — sandbox is the boundary.  Configure _perms with
    # no verifier/keystore so all scopes fall back to god-mode (["*"]).
    _perms.configure()

    # Ensure dirs exist (bwrap creates the mount points, but the user's
    # knowledge dir may be empty on first connection).
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    search_index = SearchIndex()
    search_index.build(KNOWLEDGE_DIR)

    rel_graph = RelationshipGraph()
    rel_graph.build(KNOWLEDGE_DIR)

    register_knowledge_tools(mcp, KNOWLEDGE_DIR, search_index=search_index, rel_graph=rel_graph)
    register_maintain_tools(mcp, KNOWLEDGE_DIR)
    register_meta_tools(mcp, KNOWLEDGE_DIR)
    register_inbox_tools(mcp, KNOWLEDGE_DIR)
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

    mcp = _build_worker_mcp()

    # Build ASGI app with Streamable HTTP transport (same pattern as
    # server.py) and serve via uvicorn on a Unix domain socket.
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    inner = mcp.streamable_http_app()

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET", "HEAD"]),
            *inner.routes,
        ],
        middleware=inner.user_middleware,
        lifespan=lifespan,
    )

    # Remove stale socket from a previous crash.
    socket_path.unlink(missing_ok=True)

    import uvicorn

    logger.info("worker: starting on socket %s", socket_path)
    uvicorn.run(app, uds=str(socket_path), log_level="info")


if __name__ == "__main__":
    main()
