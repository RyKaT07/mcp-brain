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

Multi-user helpers
------------------
`get_current_user_id()` resolves the user_id associated with the active
bearer token — used by knowledge tools to scope files into
`knowledge/users/{user_id}/` for multi-user isolation. Returns None for
yaml tokens that have no user_id (Patryk's single-user default).

`meter_call(tool_name)` records one call against the active key in the
UsageMeter, if one is registered. Register at startup via
`configure(yaml_user_index, key_store, usage_meter)`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from mcp.server.auth.middleware.auth_context import get_access_token

from mcp_brain.auth import PermissionDenied, match_scope

if TYPE_CHECKING:
    from mcp_brain.auth import YamlTokenVerifier
    from mcp_brain.keystore import KeyStore
    from mcp_brain.usage import UsageMeter


class _AllSentinel:
    """Sentinel meaning 'wildcard — every sub-scope is allowed'."""

    def __repr__(self) -> str:
        return "ALL"

    def __contains__(self, _item: object) -> bool:
        return True


ALL: Final = _AllSentinel()

# ------------------------------------------------------------------
# Module-level state injected at server startup via configure()

_yaml_verifier: "YamlTokenVerifier | None" = None  # live reference for hot-reload
_key_store: "KeyStore | None" = None
_usage_meter: "UsageMeter | None" = None


def configure(
    yaml_user_index: dict[str, str] | None = None,
    key_store: "KeyStore | None" = None,
    usage_meter: "UsageMeter | None" = None,
    yaml_verifier: "YamlTokenVerifier | None" = None,
) -> None:
    """Register multi-user helpers.  Called once from server.py at startup.

    Args:
        yaml_user_index: Deprecated — ignored. Pass yaml_verifier instead
            so get_current_user_id() reads the live config on every call
            and is not affected by hot-reloads that add/change user_id entries.
        yaml_verifier: The live YamlTokenVerifier, if running with yaml auth.
            get_current_user_id() reads verifier.config directly to avoid the
            stale-index IDOR: a yaml token added/changed after startup would
            otherwise route to the wrong knowledge directory until restart.
        key_store:  The dynamic KeyStore, if running with one.
        usage_meter: The UsageMeter, if usage tracking is enabled.
    """
    global _yaml_verifier, _key_store, _usage_meter
    _yaml_verifier = yaml_verifier
    _key_store = key_store
    _usage_meter = usage_meter


# ------------------------------------------------------------------
# Core scope helpers

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


# ------------------------------------------------------------------
# Multi-user helpers

def get_current_user_id() -> str | None:
    """Return the user_id for the active bearer token, or None.

    Lookup order:
    1. stdio mode (no token) → None (god-mode, Patryk's single-user path)
    2. yaml token with user_id set → from live yaml_verifier.config (not a
       stale snapshot) to prevent IDOR when tokens are added/changed at runtime
    3. dynamic keystore token → from KeyStore.by_id()
    4. yaml token without user_id, or OAuth token → None (root knowledge/)

    Returning None means "use the root knowledge/ dir" — that is the
    backward-compatible default for Patryk's existing setup.
    """
    tok = get_access_token()
    if tok is None:
        return None  # stdio / god-mode
    client_id = tok.client_id
    # 1. yaml token with explicit user_id — read live config to avoid stale index
    if _yaml_verifier is not None:
        for entry in _yaml_verifier.config.tokens:
            if entry.id == client_id and entry.user_id is not None:
                return entry.user_id
    # 2. dynamic keystore token
    if _key_store is not None:
        entry = _key_store.by_id(client_id)
        if entry is not None and entry.is_active:
            return entry.user_id
    return None


def get_effective_knowledge_dir(base: Path) -> Path:
    """Return the knowledge dir for the active token.

    - No user_id → `base` (the root knowledge/ dir, Patryk's default)
    - user_id set → `base / "users" / user_id`
    """
    user_id = get_current_user_id()
    if user_id is None:
        return base
    return base / "users" / user_id


def meter_call(tool_name: str) -> None:
    """Record one tool call against the active key's usage counter.

    No-ops if:
    - No UsageMeter is registered (usage tracking not configured)
    - Running in stdio mode (no access token, no key_id to charge)
    - Token is a yaml or OAuth token with no keystore entry (those are
      static configs; we only meter dynamic API keys for billing)
    """
    if _usage_meter is None:
        return
    tok = get_access_token()
    if tok is None:
        return  # stdio
    # Only meter dynamic keystore keys (those have entries in the store)
    if _key_store is None:
        return
    entry = _key_store.by_id(tok.client_id)
    if entry is None:
        return
    _usage_meter.record(entry.id, tool_name)
