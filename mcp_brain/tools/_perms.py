"""
Permission helpers for tools.

Tools call `require("knowledge:write:school")` at the top of their body.
If the active bearer token (taken from FastMCP's auth context) does not
match the required scope, `PermissionDenied` is raised — tools catch
that and return a human-readable error string instead of crashing the
MCP call (better LLM UX than a 500).

For list/briefing tools that should *filter* their output rather than
deny outright, use `allowed_subscopes("knowledge:read")` to get the set
of permitted sub-scopes (or `ALL` if the token has a matching wildcard).
"""

from __future__ import annotations

from typing import Final

from mcp.server.auth.middleware.auth_context import get_access_token

from mcp_brain.auth import PermissionDenied, match_scope


class _AllSentinel:
    """Sentinel meaning 'wildcard — every sub-scope is allowed'."""

    def __repr__(self) -> str:
        return "ALL"

    def __contains__(self, _item: object) -> bool:
        return True


ALL: Final = _AllSentinel()


def _current_scopes() -> list[str]:
    tok = get_access_token()
    if tok is None:
        # stdio mode (no HTTP, no auth) — treat as god mode for local dev
        return ["*"]
    return list(tok.scopes)


def has_scope(required: str) -> bool:
    return any(match_scope(g, required) for g in _current_scopes())


def require(*required_scopes: str) -> None:
    """Raise PermissionDenied unless the token has every listed scope."""
    for req in required_scopes:
        if not has_scope(req):
            raise PermissionDenied(req)


def allowed_subscopes(prefix: str) -> _AllSentinel | set[str]:
    """Return the set of sub-scopes for which the token has `prefix:<x>`.

    If the token has any matching wildcard (`*`, `prefix:*`, etc.) the
    sentinel `ALL` is returned — callers should treat that as 'no filter'.
    """
    granted = _current_scopes()
    expected_segments = prefix.count(":") + 2  # prefix + one extra segment
    result: set[str] = set()
    for g in granted:
        if g == "*":
            return ALL
        parts = g.split(":")
        if len(parts) != expected_segments:
            continue
        prefix_parts = prefix.split(":")
        if any(pp != "*" and pp != gp for pp, gp in zip(prefix_parts, parts[:-1])):
            continue
        last = parts[-1]
        if last == "*":
            return ALL
        result.add(last)
    return result
