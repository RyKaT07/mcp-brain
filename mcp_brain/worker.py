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
- Knowledge, maintain, briefing AND integration tools are registered.
  Integration tools (Todoist, Trello, etc.) are conditionally loaded
  based on env vars inherited from the parent container.
- No OAuth, no admin routes, no CSP middleware.

Usage (from bwrap command line):
    python3 -m mcp_brain.worker --socket /run/mcp-brain/sockets/{user_id}.sock
"""

from __future__ import annotations

import argparse
import asyncio
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
    Indexes are built asynchronously after the server starts so the socket
    appears immediately and the manager doesn't time out.
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

    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "mcp-brain-worker",
        transport_security=TransportSecuritySettings(
            # Worker listens on a Unix domain socket behind the isolation
            # manager — no network exposure.  Disable DNS rebinding
            # protection so the proxy's Host: header isn't rejected.
            enable_dns_rebinding_protection=False,
        ),
    )

    # No auth in worker — sandbox is the boundary.  Configure _perms with
    # no verifier/keystore so all scopes fall back to god-mode (["*"]).
    _perms.configure()

    # Ensure dirs exist (bwrap creates the mount points, but the user's
    # knowledge dir may be empty on first connection).
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Create indexes backed by files in STATE_DIR so they persist across
    # worker restarts and idle evictions.  build() will skip the rebuild
    # if the knowledge directory hasn't changed (fingerprint match).
    search_index = SearchIndex(db_path=STATE_DIR / "search.db")
    rel_graph = RelationshipGraph(db_path=STATE_DIR / "graph.db")

    register_knowledge_tools(mcp, KNOWLEDGE_DIR, search_index=search_index, rel_graph=rel_graph)
    register_maintain_tools(mcp, KNOWLEDGE_DIR)
    register_meta_tools(mcp, KNOWLEDGE_DIR)
    register_inbox_tools(mcp, KNOWLEDGE_DIR)
    register_briefing_tools(mcp, KNOWLEDGE_DIR)
    register_search_tools(mcp, KNOWLEDGE_DIR, search_index)
    register_graph_tools(mcp, KNOWLEDGE_DIR, rel_graph)

    # ── Integration tools (conditional on env vars) ────────────────────────
    todoist_key = os.getenv("TODOIST_API_KEY", "")
    if todoist_key:
        from mcp_brain.tools.todoist import register_todoist_tools
        register_todoist_tools(mcp, todoist_key)

    trello_key = os.getenv("TRELLO_API_KEY", "")
    trello_token = os.getenv("TRELLO_API_TOKEN", "")
    if trello_key and trello_token:
        from mcp_brain.tools.trello import register_trello_tools
        register_trello_tools(mcp, trello_key, trello_token)

    nc_url = os.getenv("NEXTCLOUD_URL", "")
    nc_user = os.getenv("NEXTCLOUD_USER", "")
    nc_pass = os.getenv("NEXTCLOUD_PASSWORD", "")
    if nc_url and nc_user and nc_pass:
        from mcp_brain.tools.nextcloud import register_nextcloud_tools
        register_nextcloud_tools(mcp, nc_url, nc_user, nc_pass)

    gcal_client = os.getenv("GOOGLE_CLIENT_ID", "")
    gcal_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    gcal_refresh = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if gcal_client and gcal_secret and gcal_refresh:
        from mcp_brain.tools.gcal import register_gcal_tools
        register_gcal_tools(mcp, gcal_client, gcal_secret, gcal_refresh)

    # brain_wake registered last so its inventory snapshot is complete.
    register_wake_tools(mcp, KNOWLEDGE_DIR)

    return mcp, search_index, rel_graph


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

    mcp, search_index, rel_graph = _build_worker_mcp()

    # Build ASGI app with Streamable HTTP transport (same pattern as
    # server.py) and serve via uvicorn on a Unix domain socket.
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    inner = mcp.streamable_http_app()

    _index_ready = asyncio.Event()

    async def _build_indexes_background():
        """Build search and graph indexes in a background thread after server starts."""
        loop = asyncio.get_running_loop()
        logger.info("worker: building indexes in background...")
        try:
            await loop.run_in_executor(None, search_index.build, KNOWLEDGE_DIR)
            await loop.run_in_executor(None, rel_graph.build, KNOWLEDGE_DIR)
            logger.info("worker: indexes ready")
        except Exception:
            logger.exception("worker: index build failed")
        finally:
            _index_ready.set()

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "indexes_ready": _index_ready.is_set(),
        })

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            # Start index build as a background task — server is already listening.
            asyncio.create_task(_build_indexes_background(), name="index-build")
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
