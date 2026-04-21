"""
Isolation manager entrypoint for production multi-user mode.

When ``MCP_ISOLATION=bwrap`` this module is used as the main server entrypoint
instead of the shared server in ``server.py``.  Architecture:

- One Starlette app (outer) listens on ``MCP_HOST:MCP_PORT`` (default 0.0.0.0:8400).
- Incoming requests carry a bearer token that is validated here, before any
  worker is touched.
- A per-user bwrap-sandboxed worker is spawned on demand via ProcessManager.
  Each worker is a full MCP HTTP server on its own Unix domain socket.
- The request is proxied verbatim to the worker's socket using an httpx
  AsyncClient with a Unix socket transport.
- StreamingResponse is used for the return path so SSE streams pass through
  without buffering.

Token validation order:
1. YamlTokenVerifier (static auth.yaml) — resolves user_id from entry.user_id.
2. KeyStore (dynamic API keys) — resolves user_id from KeyEntry.user_id.
3. Unauthenticated or token without a user_id → 401 / 403.

Security note: workers run without auth middleware and trust the sandbox
boundary.  The entrypoint is the only place where tokens are validated.  Only
requests that pass validation and carry a user_id are forwarded to a worker.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mcp_brain.auth import YamlTokenVerifier
from mcp_brain.isolation.manager import ProcessManager
from mcp_brain.keystore import KeyStore

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Root dirs for per-user knowledge and state.  Each user gets a sub-directory
# named after their user_id (e.g. knowledge/users/alice/).
KNOWLEDGE_BASE = Path(os.getenv("MCP_KNOWLEDGE_BASE", "./knowledge/users"))
STATE_BASE = Path(os.getenv("MCP_STATE_BASE", "./data/state/users"))
SOCKET_DIR = Path(os.getenv("MCP_SOCKET_DIR", "/run/mcp-brain/sockets"))
AUTH_CONFIG_PATH = Path(os.getenv("MCP_AUTH_CONFIG", "./config/auth.yaml"))
KEY_STORE_PATH = Path(os.getenv("MCP_KEY_STORE", "./data/keys.json"))
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8400"))
IDLE_TIMEOUT = int(os.getenv("MCP_IDLE_TIMEOUT", "600"))

# Static bearer token required on ``/admin/*`` endpoints.  When unset the admin
# surface is disabled (fail-closed) — useful in stdio / self-hosted single-user
# setups where no panel ever talks to the brain.
ADMIN_TOKEN = os.getenv("BRAIN_ADMIN_TOKEN", "").strip()

# Headers that must not be forwarded to the worker (hop-by-hop / transport-level).
_HOP_BY_HOP = frozenset({
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
})


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _extract_bearer(request: Request) -> str | None:
    """Extract the bearer token value from the Authorization header."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def _resolve_user_id(
    token: str,
    yaml_verifier: YamlTokenVerifier,
    key_store: KeyStore,
) -> str | None:
    """Return the user_id for a valid token, or None on auth failure.

    None is also returned for tokens that are valid but have no user_id
    (Patryk's single-user yaml tokens).  Those are not allowed in isolation
    mode — every caller must be mapped to an isolated directory.

    Returns:
        user_id string on success.
        None if the token is invalid, revoked, or has no user_id.
    """
    # 1. Try yaml config (hot-reload-aware, returns AccessToken or None).
    access = await yaml_verifier.verify_token(token)
    if access is not None:
        for entry in yaml_verifier.config.tokens:
            if entry.id == access.client_id:
                return entry.user_id  # None if user_id not configured for this token
        return None

    # 2. Try dynamic keystore.
    ks_entry = key_store.by_token(token)
    if ks_entry is not None:
        return ks_entry.user_id  # always set on dynamic keys

    return None  # unknown token


# ── Proxy helpers ─────────────────────────────────────────────────────────────

def _proxy_request_headers(request: Request) -> dict[str, str]:
    """Build forwarded headers, stripping hop-by-hop entries."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _proxy_response_headers(response: httpx.Response) -> dict[str, str]:
    """Build response headers to relay, stripping hop-by-hop entries."""
    return {
        k: v
        for k, v in response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


# ── Admin helpers ─────────────────────────────────────────────────────────────

def _check_admin(request: Request) -> Response | None:
    """Return an error response if the request lacks a valid admin token.

    Fail-closed: if ``ADMIN_TOKEN`` is unset the admin surface is disabled and
    every request is rejected with 404 so its existence can't be probed.
    """
    if not ADMIN_TOKEN:
        return JSONResponse({"error": "not found"}, status_code=404)
    token = _extract_bearer(request)
    # Constant-time comparison to avoid timing leaks on the admin token.
    if token is None or not _consttime_eq(token, ADMIN_TOKEN):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="mcp-brain-admin"'},
        )
    return None


def _consttime_eq(a: str, b: str) -> bool:
    """Constant-time string equality — avoids early-exit timing leaks."""
    if len(a) != len(b):
        return False
    result = 0
    for ca, cb in zip(a, b):
        result |= ord(ca) ^ ord(cb)
    return result == 0


# ── ASGI app factory ──────────────────────────────────────────────────────────

def build_app(
    yaml_verifier: YamlTokenVerifier,
    key_store: KeyStore,
    process_manager: ProcessManager,
) -> Starlette:
    """Return the Starlette ASGI app for the isolation manager."""

    # ── /healthz ─────────────────────────────────────────────────────────────

    async def healthz(request: Request) -> JSONResponse:
        """Shallow health check — returns manager status and active worker count."""
        snapshot = process_manager.list_workers()
        active_count = sum(1 for w in snapshot if w["alive"])
        return JSONResponse({"status": "ok", "active_workers": active_count})

    # ── /admin — Panel → brain control surface ────────────────────────────────
    #
    # Gated by BRAIN_ADMIN_TOKEN (fail-closed when unset).  These endpoints let
    # the Panel react to user-initiated config changes without restarting the
    # container or touching individual workers from outside.

    async def admin_status(request: Request) -> Response:
        err = _check_admin(request)
        if err is not None:
            return err
        snapshot = process_manager.list_workers()
        return JSONResponse({
            "workers": snapshot,
            "count": len(snapshot),
            "active": sum(1 for w in snapshot if w["alive"]),
        })

    async def admin_reload_user(request: Request) -> Response:
        """Kill the worker for the given user so the next request respawns it.

        Used by the Panel after rewriting that user's ``integrations.env`` to
        ensure the new credentials take effect without a container restart.
        """
        err = _check_admin(request)
        if err is not None:
            return err
        user_id = request.path_params["user_id"]
        if not user_id or "/" in user_id or ".." in user_id:
            return JSONResponse({"error": "invalid user_id"}, status_code=400)
        killed = await process_manager.kill_worker(user_id)
        return JSONResponse({"ok": True, "user_id": user_id, "killed": killed})

    # ── catch-all proxy ───────────────────────────────────────────────────────

    async def proxy(request: Request) -> Response:
        """Validate token → resolve user → ensure worker → proxy request."""

        # 1. Token extraction.
        token = _extract_bearer(request)
        if not token:
            return JSONResponse(
                {"error": "missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mcp-brain"'},
            )

        # 2. Resolve user_id.
        user_id = await _resolve_user_id(token, yaml_verifier, key_store)
        if user_id is None:
            logger.warning("entrypoint: rejected request — invalid token or no user_id")
            return JSONResponse(
                {"error": "invalid token or token has no user_id"},
                status_code=403,
            )

        # 3. Ensure worker is running.
        try:
            worker = await process_manager.get_or_spawn(user_id)
        except RuntimeError as exc:
            logger.error("entrypoint: failed to spawn worker for user=%s: %s", user_id, exc)
            return JSONResponse({"error": "worker unavailable"}, status_code=503)

        # Touch last_activity so the idle reaper does not evict this worker
        # during a long-running SSE session.
        process_manager.touch(user_id)

        # 4. Proxy the request.
        target_path = request.url.path
        if request.url.query:
            target_path = f"{target_path}?{request.url.query}"

        headers = _proxy_request_headers(request)
        body = await request.body()

        transport = httpx.AsyncHTTPTransport(uds=str(worker.socket_path))
        # base_url must be an HTTP URL even though the connection is a UDS —
        # httpx rewrites the host header but the transport ignores it.
        client = httpx.AsyncClient(
            transport=transport,
            base_url="http://worker",
            timeout=None,  # MCP SSE sessions are long-lived
        )

        try:
            upstream_req = client.build_request(
                method=request.method,
                url=target_path,
                headers=headers,
                content=body,
            )
            upstream_resp = await client.send(upstream_req, stream=True)
        except httpx.ConnectError as exc:
            await client.aclose()
            logger.error(
                "entrypoint: cannot connect to worker socket user=%s path=%s: %s",
                user_id,
                worker.socket_path,
                exc,
            )
            return JSONResponse({"error": "worker connection failed"}, status_code=503)
        except Exception as exc:
            await client.aclose()
            logger.exception("entrypoint: proxy error user=%s: %s", user_id, exc)
            return JSONResponse({"error": "proxy error"}, status_code=502)

        resp_headers = _proxy_response_headers(upstream_resp)

        # 5. Stream response back (handles both SSE and regular JSON-RPC replies).
        async def _stream_body():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    # Keep last_activity current throughout long SSE sessions.
                    process_manager.touch(user_id)
                    yield chunk
            finally:
                await upstream_resp.aclose()
                await client.aclose()

        return StreamingResponse(
            _stream_body(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    # ── Lifespan: start reaper, shut down workers on exit ────────────────────

    @asynccontextmanager
    async def lifespan(_app):
        await process_manager.start()
        logger.info(
            "isolation manager: listening on %s:%d, socket_dir=%s",
            HOST,
            PORT,
            SOCKET_DIR,
        )
        try:
            yield
        finally:
            logger.info("isolation manager: shutting down workers")
            await process_manager.shutdown()

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET", "HEAD"]),
            Route("/health", healthz, methods=["GET", "HEAD"]),
            # Admin surface — must be registered before the catch-all proxy.
            Route("/admin/status", admin_status, methods=["GET"]),
            Route("/admin/reload-user/{user_id}", admin_reload_user, methods=["POST"]),
            # Catch-all proxy: all other paths (including /mcp) go to the worker.
            Route("/{path:path}", proxy, methods=["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]),
        ],
        lifespan=lifespan,
    )


# ── Main entrypoint ───────────────────────────────────────────────────────────

def main() -> None:
    """Start the isolation manager.

    Called from ``server.py`` when ``MCP_ISOLATION=bwrap``.
    """
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _startup_log = _logging.getLogger(__name__)

    # Ensure required directories exist.
    for d in (SOCKET_DIR, KNOWLEDGE_BASE, STATE_BASE):
        d.mkdir(parents=True, exist_ok=True)

    # Support inline auth config via env var (same as server.py).
    _auth_yaml_inline = os.getenv("MCP_AUTH_YAML", "")
    if _auth_yaml_inline:
        AUTH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTH_CONFIG_PATH.write_text(_auth_yaml_inline, encoding="utf-8")
        os.chmod(AUTH_CONFIG_PATH, 0o600)

    yaml_verifier = YamlTokenVerifier(AUTH_CONFIG_PATH)
    key_store = KeyStore(KEY_STORE_PATH)
    process_manager = ProcessManager(
        knowledge_base=KNOWLEDGE_BASE,
        state_base=STATE_BASE,
        socket_dir=SOCKET_DIR,
        idle_timeout=IDLE_TIMEOUT,
    )

    app = build_app(yaml_verifier, key_store, process_manager)

    if HOST == "0.0.0.0":
        _startup_log.warning(
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  WARNING: isolation manager binding to 0.0.0.0          ║\n"
            "║  The server is reachable on ALL network interfaces.      ║\n"
            "║  Never expose mcp-brain directly to the internet.        ║\n"
            "║  Use a reverse proxy (nginx/Caddy) with TLS in front.    ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )

    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
