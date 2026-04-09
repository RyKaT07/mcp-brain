"""
OAuth 2.1 authorization server for claude.ai Custom Connectors.

mcp-brain runs TWO auth modes simultaneously:

- **yaml bearer tokens** (`YamlTokenVerifier`, `config/auth.yaml`) — for
  Claude Code CLI which lets the user paste any `Authorization` header
  into `~/.claude.json`. Tokens are configured by hand and never
  rotate automatically.
- **OAuth-issued tokens** (this module) — for claude.ai web + iOS +
  Android + desktop chat "Custom Connectors", whose dialog only
  supports OAuth 2.0. Every claude.ai device runs through DCR +
  authorization_code + PKCE to get a `tok_oauth_*` bearer on our
  server, and refreshes via `refresh_token` exchange until revoked.

Both live behind a single `ChainedProvider.load_access_token()` which
first checks the OAuth store on disk, then falls back to the yaml
verifier. Tools see no difference — both paths land in
`BearerAuthBackend`'s `AuthenticatedUser.scopes` and flow through
`_perms.require()` the same way.

This Phase 1 scaffold implements `load_access_token` fully (the bearer
validation chain) and leaves the DCR/authorize/token/refresh methods
raising `NotImplementedError`. Phase 2 fills them in.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel, Field

from mcp_brain.auth import YamlTokenVerifier

logger = logging.getLogger("mcp_brain.oauth")

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

ACCESS_TOKEN_TTL_S: Final = 60 * 60 * 24 * 7  # 7 days
AUTH_CODE_TTL_S: Final = 60                   # 1 minute
PENDING_CONSENT_TTL_S: Final = 60 * 5         # 5 minutes


# -----------------------------------------------------------------------------
# Persistent on-disk records
# -----------------------------------------------------------------------------


class AccessTokenRecord(BaseModel):
    """One OAuth-issued access token. Persisted to disk."""

    token: str
    client_id: str
    scopes: list[str]
    expires_at: int
    resource: str | None = None


class RefreshTokenRecord(BaseModel):
    """One OAuth-issued refresh token. Rotated on every exchange."""

    token: str
    client_id: str
    scopes: list[str]
    expires_at: int | None = None
    resource: str | None = None


class OAuthStoreModel(BaseModel):
    """
    On-disk JSON schema for persistent OAuth state.

    Lives at `MCP_OAUTH_STORE` (default `/data/oauth-state.json`).
    Clients and issued tokens survive container restarts; auth codes
    and pending consents are RAM-only (short TTL).
    """

    clients: dict[str, OAuthClientInformationFull] = Field(default_factory=dict)
    access_tokens: dict[str, AccessTokenRecord] = Field(default_factory=dict)
    refresh_tokens: dict[str, RefreshTokenRecord] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Persistent store with atomic JSON write
# -----------------------------------------------------------------------------


class OAuthStore:
    """
    Persistent JSON store for OAuth state with in-memory cache.

    Atomic writes via write-to-`.tmp` + `os.replace`, protected by
    `fcntl.flock` on the tempfile during write. Single-process server,
    so the lock is paranoia rather than necessity, but it's cheap.

    Loads once at construction; every mutation writes through to disk.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._data = OAuthStoreModel()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._data = OAuthStoreModel()
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "oauth store at %s is unreadable (%s); starting empty "
                "— existing clients and tokens will be lost",
                self._path,
                exc,
            )
            self._data = OAuthStoreModel()
            return
        try:
            self._data = OAuthStoreModel.model_validate(raw)
        except Exception as exc:
            logger.error(
                "oauth store at %s failed schema validation (%s); "
                "starting empty",
                self._path,
                exc,
            )
            self._data = OAuthStoreModel()
            return
        logger.info(
            "oauth store loaded: %d clients, %d access tokens, %d refresh tokens",
            len(self._data.clients),
            len(self._data.access_tokens),
            len(self._data.refresh_tokens),
        )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._prune_expired()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = self._data.model_dump(mode="json")
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, self._path)

    def _prune_expired(self) -> None:
        now = int(time.time())
        expired_access = [
            t for t, rec in self._data.access_tokens.items()
            if rec.expires_at < now
        ]
        for t in expired_access:
            del self._data.access_tokens[t]
        expired_refresh = [
            t for t, rec in self._data.refresh_tokens.items()
            if rec.expires_at is not None and rec.expires_at < now
        ]
        for t in expired_refresh:
            del self._data.refresh_tokens[t]

    # -- clients --

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._data.clients.get(client_id)

    def put_client(self, client: OAuthClientInformationFull) -> None:
        if client.client_id is None:
            raise ValueError("client.client_id must be set before storing")
        self._data.clients[client.client_id] = client
        self._save()

    # -- access tokens --

    def get_access_token(self, token: str) -> AccessTokenRecord | None:
        rec = self._data.access_tokens.get(token)
        if rec is None:
            return None
        if rec.expires_at < int(time.time()):
            return None
        return rec

    def put_access_token(self, rec: AccessTokenRecord) -> None:
        self._data.access_tokens[rec.token] = rec
        self._save()

    def delete_access_token(self, token: str) -> bool:
        if token in self._data.access_tokens:
            del self._data.access_tokens[token]
            self._save()
            return True
        return False

    def delete_access_tokens_for_client(self, client_id: str) -> int:
        to_delete = [
            t for t, rec in self._data.access_tokens.items()
            if rec.client_id == client_id
        ]
        for t in to_delete:
            del self._data.access_tokens[t]
        if to_delete:
            self._save()
        return len(to_delete)

    # -- refresh tokens --

    def get_refresh_token(self, token: str) -> RefreshTokenRecord | None:
        rec = self._data.refresh_tokens.get(token)
        if rec is None:
            return None
        if rec.expires_at is not None and rec.expires_at < int(time.time()):
            return None
        return rec

    def put_refresh_token(self, rec: RefreshTokenRecord) -> None:
        self._data.refresh_tokens[rec.token] = rec
        self._save()

    def delete_refresh_token(self, token: str) -> bool:
        if token in self._data.refresh_tokens:
            del self._data.refresh_tokens[token]
            self._save()
            return True
        return False


# -----------------------------------------------------------------------------
# In-memory transient stores (authorization codes, pending consents)
# -----------------------------------------------------------------------------


@dataclass
class PendingConsent:
    """In-memory entry awaiting user approval at /oauth/consent.

    Created by `ChainedProvider.authorize()`, popped by the consent
    route when the user posts back with the admin secret.
    """

    pending_id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float


class PendingConsentStore:
    """RAM-only pending consent entries with a 5-minute TTL."""

    def __init__(self) -> None:
        self._entries: dict[str, PendingConsent] = {}

    def put(self, entry: PendingConsent) -> None:
        self._prune()
        self._entries[entry.pending_id] = entry

    def pop(self, pending_id: str) -> PendingConsent | None:
        self._prune()
        return self._entries.pop(pending_id, None)

    def get(self, pending_id: str) -> PendingConsent | None:
        self._prune()
        return self._entries.get(pending_id)

    def _prune(self) -> None:
        cutoff = time.time() - PENDING_CONSENT_TTL_S
        expired = [k for k, v in self._entries.items() if v.created_at < cutoff]
        for k in expired:
            del self._entries[k]


class AuthorizationCodeStore:
    """RAM-only authorization codes with a 60 s TTL, single-use."""

    def __init__(self) -> None:
        self._entries: dict[str, AuthorizationCode] = {}

    def put(self, code: AuthorizationCode) -> None:
        self._prune()
        self._entries[code.code] = code

    def pop(self, code: str) -> AuthorizationCode | None:
        self._prune()
        return self._entries.pop(code, None)

    def get(self, code: str) -> AuthorizationCode | None:
        self._prune()
        return self._entries.get(code)

    def _prune(self) -> None:
        now = time.time()
        expired = [c for c, v in self._entries.items() if v.expires_at < now]
        for c in expired:
            del self._entries[c]


# -----------------------------------------------------------------------------
# ChainedProvider — the OAuthAuthorizationServerProvider implementation
# -----------------------------------------------------------------------------


class ChainedProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """
    OAuthAuthorizationServerProvider that serves BOTH OAuth-issued
    tokens and legacy yaml bearer tokens through a single interface.

    FastMCP wires this into BearerAuthBackend via ProviderTokenVerifier,
    which calls `load_access_token(token)` for every incoming bearer.
    Our implementation checks the OAuth store first, then falls back
    to the wrapped YamlTokenVerifier — so the laptop CLI bearer flow
    is untouched, while claude.ai Custom Connector gets its own
    dynamically-issued tokens.

    Phase 1 implements only `load_access_token` (the chain). The
    rest of the provider methods raise NotImplementedError until
    Phase 2 fills them in along with the consent page.
    """

    def __init__(
        self,
        store_path: Path,
        yaml_verifier: YamlTokenVerifier,
        admin_secret: str,
        public_url: str,
        access_token_ttl_s: int = ACCESS_TOKEN_TTL_S,
    ):
        self.store = OAuthStore(store_path)
        self.yaml_verifier = yaml_verifier
        self.admin_secret = admin_secret
        self.public_url = str(public_url).rstrip("/") + "/"
        self.access_token_ttl_s = access_token_ttl_s
        self.pending = PendingConsentStore()
        self.auth_codes = AuthorizationCodeStore()

    # ------------------------------------------------------------------ the chain

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Bearer validation for BOTH auth modes.

        1. OAuth-issued tokens live in `self.store.access_tokens`.
           If we find one that hasn't expired, return it.
        2. Otherwise delegate to the yaml verifier — the existing
           laptop-CLI bearer path.
        3. Neither matches → return None → FastMCP's BearerAuthBackend
           returns 401.
        """
        oauth_rec = self.store.get_access_token(token)
        if oauth_rec is not None:
            return AccessToken(
                token=oauth_rec.token,
                client_id=oauth_rec.client_id,
                scopes=list(oauth_rec.scopes),
                expires_at=oauth_rec.expires_at,
                resource=oauth_rec.resource,
            )
        return await self.yaml_verifier.verify_token(token)

    # ------------------------------------------------------------------ Phase 2 stubs

    async def get_client(
        self, client_id: str
    ) -> OAuthClientInformationFull | None:
        raise NotImplementedError("OAuth phase 2: get_client")

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        raise NotImplementedError("OAuth phase 2: register_client")

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        raise NotImplementedError("OAuth phase 2: authorize")

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        raise NotImplementedError("OAuth phase 2: load_authorization_code")

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        raise NotImplementedError("OAuth phase 2: exchange_authorization_code")

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        raise NotImplementedError("OAuth phase 2: load_refresh_token")

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError("OAuth phase 2: exchange_refresh_token")

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        raise NotImplementedError("OAuth phase 2: revoke_token")
