"""
Structured JSON logging middleware for mcp-brain.

Intercepts every POST /mcp request and emits one JSON log record per
tool call with token_id, tool name, duration, and status. Passes through
non-tool JSON-RPC calls (initialize, etc.) with a lighter record.

Usage — mount before the MCP handler::

    from mcp_brain.logging_middleware import MCPLoggingMiddleware
    app = MCPLoggingMiddleware(inner_app)

The middleware uses ``logging`` so the caller controls output format via
Python's standard logging config.  For JSON output, configure a JSON
formatter such as `python-json-logger` or the stdlib-compatible formatter
below (no extra deps required).
"""

from __future__ import annotations

import json
import logging
import time
from base64 import b64decode
from typing import Any, Awaitable, Callable

logger = logging.getLogger("mcp_brain.tool_call")


# ---------------------------------------------------------------------------
# Minimal JWT decode (header.payload.sig — no verification, just reading claims)
# We only need the `kid`/`client_id` from the payload for logging purposes;
# actual verification is handled by FastMCP's auth middleware upstream.

def _extract_token_id(authorization: str | None) -> str:
    """Extract token client_id from a Bearer JWT without full verification.

    Falls back to "unknown" if parsing fails.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return "unknown"
    raw = authorization[7:]
    parts = raw.split(".")
    if len(parts) != 3:
        # Not a JWT — might be an opaque token; return a redacted prefix
        return raw[:8] + "…" if len(raw) > 8 else "opaque"
    try:
        # JWT payload is base64url — add padding if needed
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(b64decode(payload_b64))
        return str(payload.get("client_id") or payload.get("sub") or "unknown")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# ASGI middleware

class MCPLoggingMiddleware:
    """ASGI middleware that logs every MCP tool call as a structured record."""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http" or scope.get("path", "") != "/mcp":
            await self.app(scope, receive, send)
            return

        method_bytes = scope.get("method", b"").upper()
        method_str = method_bytes.decode() if isinstance(method_bytes, bytes) else method_bytes
        if method_str != "POST":
            await self.app(scope, receive, send)
            return

        # Buffer the request body so we can inspect it without consuming the
        # stream (the downstream handler needs to read it too).
        body_chunks: list[bytes] = []

        async def buffered_receive() -> dict:
            msg = await receive()
            if msg["type"] == "http.request":
                body_chunks.append(msg.get("body", b""))
                # Reconstruct message with buffered body
                return {**msg, "body": b"".join(body_chunks), "more_body": False}
            return msg

        # Parse headers for token_id
        headers = dict(scope.get("headers", []))
        auth_header = (headers.get(b"authorization") or b"").decode("utf-8", errors="replace")
        token_id = _extract_token_id(auth_header)

        # Parse body to extract RPC method + tool name
        rpc_method = "unknown"
        tool_name = None
        try:
            # Peek at the first chunk before passing to middleware chain
            first_msg = await receive()
            body_chunks.append(first_msg.get("body", b""))
            body = b"".join(body_chunks)
            payload = json.loads(body)
            rpc_method = payload.get("method", "unknown")
            if rpc_method == "tools/call":
                tool_name = (payload.get("params") or {}).get("name")
        except Exception:
            pass

        # Replace receive so downstream gets the buffered body on the first
        # call, then forwards to the real receive() for disconnect detection.
        # Without this, SSE's _listen_for_disconnect busy-loops because the
        # fake receive never returns http.disconnect.
        body_snapshot = b"".join(body_chunks)
        body_replayed = False

        async def replay_receive() -> dict:
            nonlocal body_replayed
            if not body_replayed:
                body_replayed = True
                return {"type": "http.request", "body": body_snapshot, "more_body": False}
            return await receive()

        start = time.monotonic()
        status_code = 200

        async def capture_send(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            event = "tool_call" if rpc_method == "tools/call" else "mcp_request"
            record: dict[str, Any] = {
                "event": event,
                "token_id": token_id,
                "rpc_method": rpc_method,
                "duration_ms": duration_ms,
                "status": "ok" if status_code < 400 else "error",
                "http_status": status_code,
            }
            if tool_name:
                record["tool"] = tool_name
            logger.info(json.dumps(record))
        except Exception as exc:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            logger.error(
                json.dumps({
                    "event": "mcp_request_error",
                    "token_id": token_id,
                    "rpc_method": rpc_method,
                    "tool": tool_name,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                })
            )
            raise
