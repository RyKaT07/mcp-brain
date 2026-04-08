"""
mcp-brain — personal MCP server for persistent AI memory and integrations.

Two transports:

- `stdio`: local development. Auth is bypassed (no HTTP, no headers).
  Tools see no auth context and `_perms.require()` falls back to god-mode.
- `sse`: production. FastMCP installs its bearer auth middleware around
  its SSE app, and we wrap that in an outer Starlette that exposes a
  public `/healthz` route for the Docker HEALTHCHECK and reverse proxies.
"""

import os
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
TRANSPORT = os.getenv("MCP_TRANSPORT", "sse")


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
    """Construct the FastMCP instance, with bearer auth on SSE only."""
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
    """Build an outer Starlette that mounts the FastMCP SSE app under '/'
    and exposes a public, unauthenticated '/healthz' route alongside.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Mount("/", app=mcp.sse_app()),
        ]
    )


def main():
    """Entry point. Honors MCP_TRANSPORT env (stdio or sse)."""
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    uvicorn.run(_build_app(), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
