"""
Auth — multi-token bearer with per-scope permissions.

Tokens are loaded from a YAML config file (default: ./config/auth.yaml).
Each token has an id, secret value, optional description, and a list of
scopes. Scopes use a three-segment grammar:

    <resource>:<action>:<scope>

For resources without a meaningful action distinction (inbox, briefing,
secrets_schema) the action is collapsed to a single segment:

    inbox:read              briefing:work
    inbox:write             secrets_schema:homelab

Wildcards `*` are accepted at any segment, and a single bare `*` is
god-mode (matches anything).

This module:

- Loads and validates the YAML config (`AuthConfig.load`).
- Provides `YamlTokenVerifier`, an `mcp.server.auth.provider.TokenVerifier`
  implementation that FastMCP plugs into its bearer middleware.
- Provides `match_scope`, the wildcard matcher used by tools when
  enforcing permissions.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import BaseModel, Field


class PermissionDenied(Exception):
    """Raised by tools when the active token lacks a required scope."""

    def __init__(self, required: str):
        self.required = required
        super().__init__(f"Permission denied: {required}")


class TokenEntry(BaseModel):
    """One entry in `auth.yaml` — a named token with its scopes."""

    id: str = Field(..., description="Stable identifier used in logs")
    token: str = Field(..., min_length=8, description="Secret bearer value")
    description: str | None = None
    scopes: list[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    tokens: list[TokenEntry] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> "AuthConfig":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"auth config not found: {p}")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    def by_token(self) -> dict[str, TokenEntry]:
        return {t.token: t for t in self.tokens}


def match_scope(granted: str, required: str) -> bool:
    """Return True if a granted scope string covers a required scope.

    Wildcards:
        '*'                  → matches anything (god mode)
        'knowledge:*:*'      → matches 'knowledge:read:work', etc.
        'knowledge:read:*'   → matches 'knowledge:read:work', not write
        'knowledge:read:work' → exact match only

    Mixed-arity matching is allowed only via the bare '*' god-mode entry.
    Any other length mismatch is treated as no match — this keeps the
    grammar predictable.
    """
    if granted == "*":
        return True
    g = granted.split(":")
    r = required.split(":")
    if len(g) != len(r):
        return False
    return all(gp == "*" or gp == rp for gp, rp in zip(g, r))


class YamlTokenVerifier(TokenVerifier):
    """TokenVerifier backed by an `auth.yaml` file.

    The file is read once at construction. Restart the server to pick
    up changes — single-user MCP, no hot-reload needed.
    """

    def __init__(self, config_path: Path | str):
        self._config = AuthConfig.load(config_path)
        self._index = self._config.by_token()

    @property
    def config(self) -> AuthConfig:
        return self._config

    async def verify_token(self, token: str) -> AccessToken | None:
        entry = self._index.get(token)
        if entry is None:
            return None
        return AccessToken(
            token=entry.token,
            client_id=entry.id,
            scopes=list(entry.scopes),
        )
