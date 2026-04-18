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

from mcp_brain.admin import build_admin_routes
from mcp_brain.auth import YamlTokenVerifier
from mcp_brain.integration_store import IntegrationStore
from mcp_brain.logging_middleware import MCPLoggingMiddleware
from mcp_brain.keystore import KeyStore
from mcp_brain.oauth import ChainedProvider, register_oauth_consent_route
from mcp_brain.usage import UsageMeter
from mcp_brain.tools import _perms
from mcp_brain.tools.apikeys import register_apikeys_tools
from mcp_brain.tools.briefing import register_briefing_tools
from mcp_brain.tools.inbox import register_inbox_tools
from mcp_brain.tools.knowledge import register_knowledge_tools
from mcp_brain.tools.secrets_schema import register_secrets_tools
from mcp_brain.tools.nextcloud import register_nextcloud_tools
from mcp_brain.tools.gcal import register_gcal_tools
from mcp_brain.tools.todoist import fetch_tasks_for_index, register_todoist_tools
from mcp_brain.tools.trello import fetch_cards_for_index, register_trello_tools
from mcp_brain.tools.maintain import register_maintain_tools
from mcp_brain.tools.meta import register_meta_tools
from mcp_brain.tools.search import register_search_tools
from mcp_brain.tools.graph import register_graph_tools
from mcp_brain.tools.wake import register_wake_tools
from mcp_brain.search import SearchIndex
from mcp_brain.graph import RelationshipGraph

KNOWLEDGE_DIR = Path(os.getenv("MCP_KNOWLEDGE_DIR", "./knowledge"))
AUTH_CONFIG_PATH = Path(os.getenv("MCP_AUTH_CONFIG", "./config/auth.yaml"))

# Support inline auth config via env var — used by per-user container
# provisioning where the panel passes auth.yaml content directly.
# If set, write it to AUTH_CONFIG_PATH so the rest of the startup path
# (YamlTokenVerifier) works unchanged.
_AUTH_YAML_INLINE = os.getenv("MCP_AUTH_YAML", "")
if _AUTH_YAML_INLINE:
    AUTH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_CONFIG_PATH.write_text(_AUTH_YAML_INLINE, encoding="utf-8")
    os.chmod(AUTH_CONFIG_PATH, 0o600)

OAUTH_STORE_PATH = Path(os.getenv("MCP_OAUTH_STORE", "./data/oauth-state.json"))
KEY_STORE_PATH = Path(os.getenv("MCP_KEY_STORE", "./data/keys.json"))
USAGE_STORE_PATH = Path(os.getenv("MCP_USAGE_STORE", "./data/usage.json"))
INTEGRATION_STORE_PATH = Path(os.getenv("MCP_INTEGRATION_STORE", "./data/integrations.json"))
OAUTH_ADMIN_SECRET = os.getenv("MCP_OAUTH_ADMIN_SECRET", "")
ADMIN_SECRET = os.getenv("MCP_ADMIN_SECRET", "")
PANEL_URL = os.getenv("PANEL_URL", "")
CONSENT_SIGNING_SECRET = os.getenv("CONSENT_SIGNING_SECRET", "")
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8400"))
PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", f"http://localhost:{PORT}/")
# Default transport is Streamable HTTP. "stdio" is the only other valid
# value; anything else is treated as http so legacy `MCP_TRANSPORT=sse`
# in existing .env files still boots cleanly rather than erroring out.
TRANSPORT = os.getenv("MCP_TRANSPORT", "http")
TODOIST_API_KEY = os.getenv("TODOIST_API_KEY", "")
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "")
NEXTCLOUD_USER = os.getenv("NEXTCLOUD_USER", "")
NEXTCLOUD_PASSWORD = os.getenv("NEXTCLOUD_PASSWORD", "")
TRELLO_API_KEY = os.getenv("TRELLO_API_KEY", "")
TRELLO_API_TOKEN = os.getenv("TRELLO_API_TOKEN", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")
OPENAI_VERIFICATION_TOKEN = os.getenv("MCP_OPENAI_VERIFICATION_TOKEN", "")

_CSP_VALUE = (
    "default-src 'self'; "
    "connect-src 'self' https://api.todoist.com https://api.trello.com "
    "https://*.nextcloud.* https://www.googleapis.com https://oauth2.googleapis.com"
)


class _CSPMiddleware:
    """Pure-ASGI middleware that appends a Content-Security-Policy header to all HTTP responses."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders
                headers = MutableHeaders(scope=message)
                headers.append("content-security-policy", _CSP_VALUE)
            await send(message)

        await self.app(scope, receive, _send)


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


def _load_briefing_trigger(knowledge_dir: Path) -> str:
    """Load the wake-word / trigger rules from `_meta/write-policy.md`, if any.

    Same rationale as `_load_tool_policy`: claude.ai web does not
    reliably enforce rules delivered via `instructions` alone. Injecting
    the trigger rules into the `get_briefing` tool description ensures
    the model sees them regardless of client behavior — the tool schema
    is the one channel every MCP client must pass through verbatim.

    Looks for `## Read discipline` in write-policy.md and extracts it.
    The wake-word subsection (### 5. Wake word "jarvis") lives inside
    that H2, so extracting the full H2 captures it along with the
    other read-discipline rules that inform when to call get_briefing.

    Empty string if the file or the section is missing.
    """
    policy = knowledge_dir / "_meta" / "write-policy.md"
    if not policy.exists():
        return ""
    try:
        content = policy.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _extract_h2_section(content, "Read discipline — don't over-fetch")


# Module-level stores so both _build_mcp() and _build_app() share the same
# instances without threading issues.
key_store = KeyStore(KEY_STORE_PATH)
usage_meter = UsageMeter(USAGE_STORE_PATH)
integration_store = IntegrationStore(INTEGRATION_STORE_PATH)


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
    briefing_trigger = _load_briefing_trigger(KNOWLEDGE_DIR)

    if TRANSPORT == "stdio":
        # Local dev: no HTTP, no auth. Tools fall back to god-mode.
        mcp = FastMCP(
            "mcp-brain",
            host=HOST,
            port=PORT,
            instructions=instructions,
        )
        yaml_user_index: dict[str, str] = {}
    else:
        yaml_verifier = YamlTokenVerifier(AUTH_CONFIG_PATH)
        # Build yaml_user_index: {token_entry.id: user_id} for yaml
        # tokens that have an explicit user_id set. Used by
        # _perms.get_current_user_id() for multi-user knowledge routing.
        yaml_user_index = {
            entry.id: entry.user_id
            for entry in yaml_verifier.config.tokens
            if entry.user_id is not None
        }
        provider = ChainedProvider(
            store_path=OAUTH_STORE_PATH,
            yaml_verifier=yaml_verifier,
            admin_secret=OAUTH_ADMIN_SECRET,
            public_url=PUBLIC_URL,
            key_store=key_store,
            panel_url=PANEL_URL,
            consent_signing_secret=CONSENT_SIGNING_SECRET,
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

    # Register multi-user helpers so knowledge tools can resolve user dirs
    # and meter calls against the active key.
    # Pass yaml_verifier (not the pre-built yaml_user_index snapshot) so
    # get_current_user_id() reads the live config on every call and is not
    # stale after hot-reloads that add/change user_id entries (IDOR fix).
    _perms.configure(
        key_store=key_store,
        usage_meter=usage_meter,
        yaml_verifier=yaml_verifier if TRANSPORT != "stdio" else None,
    )

    search_index = SearchIndex()
    search_index.build(KNOWLEDGE_DIR)

    # Optionally index Todoist tasks and Trello cards if API keys are present.
    # Both fetches are gracefully skipped on missing keys or API errors.
    if TODOIST_API_KEY:
        tasks = fetch_tasks_for_index(TODOIST_API_KEY)
        if tasks:
            search_index.index_todoist_tasks(tasks)
    if TRELLO_API_KEY and TRELLO_API_TOKEN:
        cards = fetch_cards_for_index(TRELLO_API_KEY, TRELLO_API_TOKEN)
        if cards:
            search_index.index_trello_cards(cards)

    rel_graph = RelationshipGraph()
    rel_graph.build(KNOWLEDGE_DIR)

    register_knowledge_tools(mcp, KNOWLEDGE_DIR, tool_policy=tool_policy, search_index=search_index, rel_graph=rel_graph)
    register_maintain_tools(mcp, KNOWLEDGE_DIR)
    register_meta_tools(mcp, KNOWLEDGE_DIR)
    register_inbox_tools(mcp, KNOWLEDGE_DIR)
    register_briefing_tools(
        mcp,
        KNOWLEDGE_DIR,
        briefing_trigger=briefing_trigger,
        gcal_client_id=GOOGLE_CLIENT_ID,
        gcal_client_secret=GOOGLE_CLIENT_SECRET,
        gcal_refresh_token=GOOGLE_REFRESH_TOKEN,
        todoist_api_key=TODOIST_API_KEY,
        trello_api_key=TRELLO_API_KEY,
        trello_api_token=TRELLO_API_TOKEN,
    )
    register_secrets_tools(mcp, KNOWLEDGE_DIR)
    register_apikeys_tools(mcp, key_store, usage_meter)

    def _get_integration_creds(name: str) -> dict[str, str] | None:
        """Check IntegrationStore first, fall back to env vars."""
        stored = integration_store.get(name)
        if stored:
            return stored
        env_map: dict[str, dict[str, str]] = {
            "todoist": {"TODOIST_API_KEY": TODOIST_API_KEY},
            "trello": {"TRELLO_API_KEY": TRELLO_API_KEY, "TRELLO_API_TOKEN": TRELLO_API_TOKEN},
            "nextcloud": {
                "NEXTCLOUD_URL": NEXTCLOUD_URL,
                "NEXTCLOUD_USER": NEXTCLOUD_USER,
                "NEXTCLOUD_PASSWORD": NEXTCLOUD_PASSWORD,
            },
            "gcal": {
                "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
                "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
                "GOOGLE_REFRESH_TOKEN": GOOGLE_REFRESH_TOKEN,
            },
        }
        creds = env_map.get(name, {})
        if all(v for v in creds.values()):
            return creds
        return None

    todoist_creds = _get_integration_creds("todoist")
    if todoist_creds:
        register_todoist_tools(mcp, todoist_creds["TODOIST_API_KEY"])

    nextcloud_creds = _get_integration_creds("nextcloud")
    if nextcloud_creds:
        register_nextcloud_tools(
            mcp,
            nextcloud_creds["NEXTCLOUD_URL"],
            nextcloud_creds["NEXTCLOUD_USER"],
            nextcloud_creds["NEXTCLOUD_PASSWORD"],
        )

    trello_creds = _get_integration_creds("trello")
    if trello_creds:
        register_trello_tools(mcp, trello_creds["TRELLO_API_KEY"], trello_creds["TRELLO_API_TOKEN"])

    gcal_creds = _get_integration_creds("gcal")
    if gcal_creds:
        register_gcal_tools(
            mcp,
            gcal_creds["GOOGLE_CLIENT_ID"],
            gcal_creds["GOOGLE_CLIENT_SECRET"],
            gcal_creds["GOOGLE_REFRESH_TOKEN"],
        )
    register_search_tools(mcp, KNOWLEDGE_DIR, search_index)
    register_graph_tools(mcp, KNOWLEDGE_DIR, rel_graph)
    # brain_wake must be registered LAST so its tool inventory snapshot
    # captures every tool registered above (including conditional ones).
    register_wake_tools(mcp, KNOWLEDGE_DIR, briefing_trigger=briefing_trigger)
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
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    # Instantiating the inner app also lazy-creates `mcp.session_manager`,
    # which the outer lifespan below needs to start.
    inner = mcp.streamable_http_app()

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def openai_verification(_request: Request) -> Response:
        if not OPENAI_VERIFICATION_TOKEN:
            return Response(status_code=404)
        return Response(content=OPENAI_VERIFICATION_TOKEN, media_type="text/plain")

    @asynccontextmanager
    async def lifespan(_app):
        # `session_manager.run()` is an async context manager that
        # starts the Streamable HTTP task group on __aenter__ and
        # shuts it down on __aexit__. Without this, every request to
        # the Streamable HTTP endpoint would fail.
        async with mcp.session_manager.run():
            yield

    import logging as _logging
    _logger = _logging.getLogger("mcp_brain")

    def _on_integration_change(name: str, credentials: dict[str, str] | None) -> None:
        if credentials:
            _logger.info("Integration '%s' configured — restart to activate", name)
        else:
            _logger.info("Integration '%s' removed — restart to deactivate", name)

    admin_routes = (
        build_admin_routes(
            key_store,
            ADMIN_SECRET,
            integration_store=integration_store,
            on_integration_change=_on_integration_change,
        )
        if ADMIN_SECRET
        else []
    )

    outer = Starlette(
        debug=inner.debug,
        routes=[
            Route("/healthz", healthz, methods=["GET", "HEAD"]),
            Route("/health", healthz, methods=["GET", "HEAD"]),
            Route("/.well-known/openai-verification", openai_verification, methods=["GET"]),
            *admin_routes,
            *inner.routes,  # /mcp (streamable HTTP) + any auth metadata routes
        ],
        middleware=inner.user_middleware,  # Bearer auth + auth context
        lifespan=lifespan,
    )

    # Wrap with structured tool-call logging.  MCPLoggingMiddleware buffers
    # the request body so it can inspect JSON-RPC method + tool name without
    # interfering with the downstream handler.
    # _CSPMiddleware wraps the outermost app so all HTTP responses — including
    # healthz, the verification endpoint, and MCP routes — get the CSP header.
    return _CSPMiddleware(MCPLoggingMiddleware(outer))  # type: ignore[return-value]


def main():
    """Entry point. Honors MCP_TRANSPORT env (`stdio` or `http`)."""
    import logging as _logging
    _startup_log = _logging.getLogger(__name__)

    if TRANSPORT == "stdio":
        # ── Security warning: stdio bypasses all authentication ─────────────
        # In stdio mode the server has no HTTP layer and no bearer token
        # middleware. _perms._current_scopes() falls back to god-mode ("*"),
        # meaning ANY local process that connects gets full unrestricted access
        # to all tools. This is intentional for single-user local dev but MUST
        # NOT be used in production or multi-user environments.
        _startup_log.warning(
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  WARNING: mcp-brain running in STDIO mode                ║\n"
            "║  Authentication is BYPASSED — all tools run as god-mode  ║\n"
            "║  Use TRANSPORT=http with bearer tokens for production     ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )
        mcp.run(transport="stdio")
        return

    # ── Security check: warn if binding to all interfaces ───────────────────
    if HOST == "0.0.0.0":
        _startup_log.warning(
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  WARNING: mcp-brain is binding to 0.0.0.0               ║\n"
            "║  The server is reachable on ALL network interfaces.      ║\n"
            "║  Never expose mcp-brain directly to the internet.        ║\n"
            "║  Use a reverse proxy (nginx/Caddy) with TLS in front.    ║\n"
            "║  Set MCP_HOST=127.0.0.1 for localhost-only binding.      ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )

    import uvicorn

    uvicorn.run(_build_app(), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
