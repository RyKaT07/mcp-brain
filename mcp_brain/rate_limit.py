"""
Reusable per-tool, per-token rate limiter.

Usage:

    from mcp_brain.rate_limit import RateLimiter

    _limiter = RateLimiter("my_tool", interval_seconds=5.0)

    def my_tool(...) -> str:
        err = _limiter.check()
        if err:
            return err
        ...

Design notes:
- Rate limit state is in-process memory — resets on server restart.
- Each RateLimiter is per-tool (separate state, separate lock).
- Within a tool, rate is tracked per `token_id` (= `access_token.client_id`).
- In stdio / god-mode (no access token) rate limiting is skipped — the
  local dev path is single-user and trusted.
"""

import threading
import time

from mcp.server.auth.middleware.auth_context import get_access_token


class RateLimiter:
    """Thread-safe, per-token rate limiter for a single MCP tool."""

    def __init__(self, tool_name: str, interval_seconds: float) -> None:
        self._tool_name = tool_name
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}  # {token_id: monotonic timestamp}

    def check(self) -> str | None:
        """Return an error string if the caller is over the rate limit, else None.

        Updates the last-call timestamp on success (i.e. the call is allowed).
        """
        tok = get_access_token()
        if tok is None:
            return None  # stdio / god-mode — no limit

        token_id = tok.client_id
        now = time.monotonic()
        with self._lock:
            last = self._last_call.get(token_id)
            if last is not None and (now - last) < self._interval:
                remaining = self._interval - (now - last)
                return (
                    f"Rate limited: {self._tool_name} may be called at most once every "
                    f"{int(self._interval)} seconds per token. "
                    f"Retry in {remaining:.1f}s."
                )
            self._last_call[token_id] = now
        return None
