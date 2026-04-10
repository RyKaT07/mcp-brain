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

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import YamlTokenVerifier
from mcp_brain.oauth import ChainedProvider, register_oauth_consent_route
from mcp_brain.tools.briefing import register_briefing_tools
from mcp_brain.tools.inbox import register_inbox_tools
from mcp_brain.tools.knowledge import register_knowledge_tools
from mcp_brain.tools.secrets_schema import register_secrets_tools
from mcp_brain.tools.todoist import register_todoist_tools

KNOWLEDGE_DIR = Path(os.getenv("MCP_KNOWLEDGE_DIR", "./knowledge"))
AUTH_CONFIG_PATH = Path(os.getenv("MCP_AUTH_CONFIG", "./config/auth.yaml"))
OAUTH_STORE_PATH = Path(os.getenv("MCP_OAUTH_STORE", "./data/oauth-state.json"))
OAUTH_ADMIN_SECRET = os.getenv("MCP_OAUTH_ADMIN_SECRET", "")
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8400"))
PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", f"http://localhost:{PORT}/")
# Default transport is Streamable HTTP. "stdio" is the only other valid
# value; anything else is treated as http so legacy `MCP_TRANSPORT=sse`
# in existing .env files still boots cleanly rather than erroring out.
TRANSPORT = os.getenv("MCP_TRANSPORT", "http")
TODOIST_API_KEY = os.getenv("TODOIST_API_KEY", "")


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


def _extract_h2_section(markdown: str, title: str) -> str:
    """Extract the body of a specific H2 section from a markdown document.

    Returns the lines between `## {title}` and the next `## ...` (or EOF),
    excluding the `## {title}` header itself. Matching is case-insensitive
    on the title text; whitespace around the `## ` prefix is tolerated but
    the heading must be H2 (not H1 or H3).

    Empty string if the section is missing. Used to pull targeted
    subsections out of the user's `_meta/write-policy.md` for injection
    into specific tool descriptions — see `_load_tool_policy` below.
    """
    target = title.strip().lower()
    in_section = False
    buf: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            if in_section:
                break  # next H2 ends the section
            if heading == target:
                in_section = True
                continue
        if in_section:
            buf.append(line)
    return "\n".join(buf).strip()


def _load_tool_policy(knowledge_dir: Path) -> str:
    """Load the `## Tool policy` section from `_meta/write-policy.md`, if any.

    Motivation: MCP `InitializeResult.instructions` (see `_load_instructions`
    above) is the canonical channel for server-wide behavioral rules, but
    not every client honors it. Claude Code CLI prepends the instructions
    to its system prompt verbatim and follows them faithfully. claude.ai
    web (via OAuth) was observed 2026-04-09 to receive the instructions,
    acknowledge them as "server system prompt", and still write to the
    knowledge store without following them — it appears to treat
    per-server instructions as informational rather than binding.

    Tool descriptions, by contrast, are part of the schema every MCP
    client MUST pass through verbatim so the model knows how to call
    the tool. Rules embedded there cannot be silently dropped.

    This loader extracts the `## Tool policy` H2 from write-policy.md
    (if present) and the server prepends it to the `knowledge_update`
    tool description at registration time. Users who want tool-level
    guardrails on claude.ai add this section to their policy file; users
    who only care about Claude Code CLI can leave it out and rely on
    `instructions` alone — this is purely additive.

    Empty string if the file or the section is missing.
    """
    policy = knowledge_dir / "_meta" / "write-policy.md"
    if not policy.exists():
        return ""
    try:
        content = policy.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _extract_h2_section(content, "Tool policy")


def _build_mcp() -> FastMCP:
    """Construct the FastMCP instance, with bearer auth on HTTP only.

    HTTP mode: we switch from the simple `token_verifier=` path to
    `auth_server_provider=ChainedProvider`. ChainedProvider implements
    the OAuthAuthorizationServerProvider Protocol so FastMCP wires in
    the full OAuth flow (DCR, /authorize, /token, metadata endpoints)
    for claude.ai Custom Connectors. Internally, ChainedProvider still
    delegates to YamlTokenVerifier for legacy yaml-configured bearers,
    so the laptop CLI path is unchanged. FastMCP's constructor rejects
    passing both `token_verifier` and `auth_server_provider`, so the
    yaml verifier is wrapped inside ChainedProvider rather than passed
    directly.

    Stdio mode: unchanged. No HTTP, no auth, tools see no access token
    and fall back to god-mode via `_perms._current_scopes()`.
    """
    instructions = _load_instructions(KNOWLEDGE_DIR)
    tool_policy = _load_tool_policy(KNOWLEDGE_DIR)

    if TRANSPORT == "stdio":
        # Local dev: no HTTP, no auth. Tools fall back to god-mode.
        mcp = FastMCP(
            "mcp-brain",
            host=HOST,
            port=PORT,
            instructions=instructions,
        )
    else:
        yaml_verifier = YamlTokenVerifier(AUTH_CONFIG_PATH)
        provider = ChainedProvider(
            store_path=OAUTH_STORE_PATH,
            yaml_verifier=yaml_verifier,
            admin_secret=OAUTH_ADMIN_SECRET,
            public_url=PUBLIC_URL,
        )
        resource_server_url = str(PUBLIC_URL).rstrip("/") + "/mcp"
        mcp = FastMCP(
            "mcp-brain",
            host=HOST,
            port=PORT,
            instructions=instructions,
            auth_server_provider=provider,
            auth=AuthSettings(
                issuer_url=PUBLIC_URL,  # type: ignore[arg-type]
                resource_server_url=resource_server_url,  # type: ignore[arg-type]
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    default_scopes=["*"],
                    valid_scopes=None,
                ),
            ),
        )
        # Custom consent page for the browser step of the OAuth flow.
        # Must be registered before `streamable_http_app()` is called
        # (i.e. before _build_app()) because custom routes are appended
        # to the inner Starlette at app-build time.
        register_oauth_consent_route(mcp, provider)

    register_knowledge_tools(mcp, KNOWLEDGE_DIR, tool_policy=tool_policy)
    register_inbox_tools(mcp, KNOWLEDGE_DIR)
    register_briefing_tools(mcp, KNOWLEDGE_DIR)
    register_secrets_tools(mcp, KNOWLEDGE_DIR)
    if TODOIST_API_KEY:
        register_todoist_tools(mcp, TODOIST_API_KEY)
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
