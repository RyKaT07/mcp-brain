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

import asyncio
import logging
import os
import threading
from pathlib import Path

import yaml
from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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
    user_id: str | None = Field(
        default=None,
        description=(
            "Optional user namespace. If set, knowledge tools scope this token "
            "to knowledge/users/{user_id}/ instead of the root knowledge/ dir. "
            "Omit for Patryk's single-user setup (backward-compatible default)."
        ),
    )


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
    """TokenVerifier backed by an ``auth.yaml`` file with hot-reload support.

    The file is read at construction and reloaded automatically when its
    mtime changes.  Two reload strategies are supported:

    - **Background thread** (default): a daemon thread polls the file every
      ``reload_interval`` seconds.  Works in stdio mode and any asyncio loop.
    - **SIGHUP**: when ``enable_sighup=True``, a SIGHUP signal triggers an
      immediate reload (useful in production containers — send
      ``kill -HUP <pid>`` after updating auth.yaml).

    Hot-reload is atomic: the index is replaced in one assignment so readers
    never see a half-loaded state.  No active connections are dropped — tokens
    that were valid keep working until the next reload.
    """

    def __init__(
        self,
        config_path: Path | str,
        *,
        reload_interval: float = 5.0,
        enable_sighup: bool = True,
    ):
        self._config_path = Path(config_path)
        self._reload_interval = reload_interval
        self._lock = threading.Lock()
        self._mtime: float = 0.0
        self._config: AuthConfig | None = None
        self._index: dict[str, TokenEntry] = {}
        self._load()

        self._watcher = threading.Thread(
            target=self._watch_loop,
            daemon=True,
            name="auth-yaml-watcher",
        )
        self._watcher.start()

        if enable_sighup:
            import signal

            try:
                signal.signal(signal.SIGHUP, self._handle_sighup)
            except (OSError, AttributeError):
                # Windows or restricted environment — skip
                pass

    # ------------------------------------------------------------------
    # Public interface

    @property
    def config(self) -> AuthConfig:
        with self._lock:
            return self._config  # type: ignore[return-value]

    async def verify_token(self, token: str) -> AccessToken | None:
        with self._lock:
            entry = self._index.get(token)
        if entry is None:
            return None
        return AccessToken(
            token=entry.token,
            client_id=entry.id,
            scopes=list(entry.scopes),
        )

    # ------------------------------------------------------------------
    # Internal reload machinery

    def _load(self) -> None:
        """Load (or reload) auth.yaml; update index atomically."""
        try:
            mtime = os.path.getmtime(self._config_path)
            config = AuthConfig.load(self._config_path)
            index = config.by_token()
            with self._lock:
                self._mtime = mtime
                self._config = config
                self._index = index
            logger.info(
                "auth.yaml loaded",
                extra={"token_count": len(config.tokens), "path": str(self._config_path)},
            )
        except Exception as exc:
            logger.error("auth.yaml reload failed: %s", exc)

    def _check_reload(self) -> None:
        """Reload if the file's mtime has changed."""
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError:
            return
        with self._lock:
            current = self._mtime
        if mtime != current:
            logger.info("auth.yaml changed — reloading")
            self._load()

    def _watch_loop(self) -> None:
        """Background daemon thread: poll mtime every reload_interval seconds."""
        while True:
            threading.Event().wait(self._reload_interval)  # interruptible sleep
            self._check_reload()

    def _handle_sighup(self, _signum: int, _frame: object) -> None:
        """SIGHUP handler: force immediate reload."""
        logger.info("SIGHUP received — reloading auth.yaml")
        self._load()
