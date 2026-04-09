"""
mcp-brain — personal MCP server for persistent AI memory and integrations.

Two transports:

- `stdio`: local development. Auth is bypassed (no HTTP, no headers).
  Tools see no auth context and `_perms.require()` falls back to god-mode.
- `http`: production. FastMCP's Streamable HTTP transport is mounted
  under an outer Starlette that exposes a public `/healthz` route for
  the Docker HEALTHCHECK and reverse proxies. Bearer auth is enforced
  by the FastMCP-provided middleware chain.

Why the explicit lifespan wiring below: FastMCP's `streamable_http_app()`
returns a Starlette app whose `lifespan` parameter calls
`session_manager.run()` to start the Streamable HTTP task group. When
you mount that inner Starlette inside an outer Starlette via `Mount()`,
Starlette does NOT propagate the inner lifespan — the session manager
would never start and every request would fail. So we instantiate the
inner app (which lazy-creates `mcp.session_manager` as a side effect),
copy its routes and middleware onto the outer app, and re-wire the
lifespan by hand on the outer Starlette. `sse_app()` had no lifespan,
which is why the previous `Mount('/', sse_app())` trick worked without
this gymnastics — SSE is now deprecated in the MCP spec, so we migrated.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import YamlTokenVerifier
from mcp_brain.tools.briefing import register_briefing_tools
from mcp_brain.tools.inbox import register_inbox_tools
from mcp_brain.tools.knowledge import register_knowledge_tools
from mcp_brain.tools.secrets_schema import register_secrets_tools

KNOWLEDGE_DIR = Path(os.getenv("MCP_KNOWLEDGE_DIR", "./knowledge"))
AUTH_CONFIG_PATH = Path(os.getenv("MCP_AUTH_CONFIG", "./config/auth.yaml"))
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8400"))
PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", f"http://localhost:{PORT}/")
# Default transport is Streamable HTTP. "stdio" is the only other valid
# value; anything else is treated as http so legacy `MCP_TRANSPORT=sse`
# in existing .env files still boots cleanly rather than erroring out.
TRANSPORT = os.getenv("MCP_TRANSPORT", "http")


def _load_instructions(knowledge_dir: Path) -> str | None:
    """Load optional write-policy instructions from `knowledge/_meta/write-policy.md`.

    If the file exists, its contents are injected into the MCP server's
    `instructions` field and become visible to every client that connects
    (via the `InitializeResult` of the MCP protocol). This lets the user
    maintain a single source of truth for write discipline — in the
    knowledge store itself — instead of copying a CLAUDE.md into every
    client config on every device.

    Missing file → returns None and the server starts with no custom
    instructions. Unreadable file → same: we never block startup on this.
    """
    policy = knowledge_dir / "_meta" / "write-policy.md"
    if not policy.exists():
        return None
    try:
        content = policy.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def _build_mcp() -> FastMCP:
    """Construct the FastMCP instance, with bearer auth on HTTP only."""
    instructions = _load_instructions(KNOWLEDGE_DIR)

    if TRANSPORT == "stdio":
        # Local dev: no HTTP, no auth. Tools fall back to god-mode.
        mcp = FastMCP(
            "mcp-brain",
            host=HOST,
            port=PORT,
            instructions=instructions,
        )
    else:
        verifier = YamlTokenVerifier(AUTH_CONFIG_PATH)
        mcp = FastMCP(
            "mcp-brain",
            host=HOST,
            port=PORT,
            instructions=instructions,
            token_verifier=verifier,
            auth=AuthSettings(
                issuer_url=PUBLIC_URL,  # type: ignore[arg-type]
                resource_server_url=PUBLIC_URL,  # type: ignore[arg-type]
            ),
        )

    register_knowledge_tools(mcp, KNOWLEDGE_DIR)
    register_inbox_tools(mcp, KNOWLEDGE_DIR)
    register_briefing_tools(mcp, KNOWLEDGE_DIR)
    register_secrets_tools(mcp, KNOWLEDGE_DIR)
    return mcp


mcp = _build_mcp()


def _build_app():
    """Build the production ASGI app: Streamable HTTP routes + `/healthz`.

    See module docstring for why we extract routes from the inner
    Streamable HTTP app instead of Mount()-ing it: the inner app's
    lifespan (which runs `session_manager.run()`) does not propagate
    across a Mount boundary, so we re-assemble the routes on the outer
    Starlette and wire the lifespan manually.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    # Instantiating the inner app also lazy-creates `mcp.session_manager`,
    # which the outer lifespan below needs to start.
    inner = mcp.streamable_http_app()

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(_app):
        # `session_manager.run()` is an async context manager that
        # starts the Streamable HTTP task group on __aenter__ and
        # shuts it down on __aexit__. Without this, every request to
        # the Streamable HTTP endpoint would fail.
        async with mcp.session_manager.run():
            yield

    return Starlette(
        debug=inner.debug,
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            *inner.routes,  # /mcp (streamable HTTP) + any auth metadata routes
        ],
        middleware=inner.user_middleware,  # Bearer auth + auth context
        lifespan=lifespan,
    )


def main():
    """Entry point. Honors MCP_TRANSPORT env (`stdio` or `http`)."""
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    uvicorn.run(_build_app(), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
