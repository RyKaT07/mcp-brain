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
import hmac
import html as _html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel, Field

from mcp_brain.auth import YamlTokenVerifier

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from mcp_brain.keystore import KeyStore

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
    Our implementation checks three sources in order:

    1. OAuth-issued tokens (claude.ai Custom Connectors via /token).
    2. YAML bearer tokens (static `config/auth.yaml`, Patryk's CLI).
    3. Dynamic keystore tokens (generated via `apikeys_create` tool).

    Tools see no difference — all three paths produce an AccessToken
    with the same structure, and `_perms.require()` enforces scopes
    identically for all sources.
    """

    def __init__(
        self,
        store_path: Path,
        yaml_verifier: YamlTokenVerifier,
        admin_secret: str,
        public_url: str,
        access_token_ttl_s: int = ACCESS_TOKEN_TTL_S,
        key_store: "KeyStore | None" = None,
    ):
        self.store = OAuthStore(store_path)
        self.yaml_verifier = yaml_verifier
        self.admin_secret = admin_secret
        self.public_url = str(public_url).rstrip("/") + "/"
        self.access_token_ttl_s = access_token_ttl_s
        self.pending = PendingConsentStore()
        self.auth_codes = AuthorizationCodeStore()
        self.key_store = key_store  # dynamic API key store (may be None)

    # ------------------------------------------------------------------ the chain

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Bearer validation for all three auth modes.

        1. OAuth-issued tokens live in `self.store.access_tokens`.
           If we find one that hasn't expired, return it.
        2. Otherwise delegate to the yaml verifier — the existing
           laptop-CLI bearer path.
        3. Check the dynamic keystore for API keys generated via
           `apikeys_create`. Uses key UUID as client_id so
           `_perms.get_current_user_id()` can map it back to user_id.
        4. None of the above matches → return None → 401.
        """
        # 1. OAuth-issued tokens
        oauth_rec = self.store.get_access_token(token)
        if oauth_rec is not None:
            return AccessToken(
                token=oauth_rec.token,
                client_id=oauth_rec.client_id,
                scopes=list(oauth_rec.scopes),
                expires_at=oauth_rec.expires_at,
                resource=oauth_rec.resource,
            )
        # 2. YAML bearer tokens
        yaml_tok = await self.yaml_verifier.verify_token(token)
        if yaml_tok is not None:
            return yaml_tok
        # 3. Dynamic keystore tokens
        if self.key_store is not None:
            key_entry = self.key_store.by_token(token)
            if key_entry is not None:
                return AccessToken(
                    token=key_entry.token,
                    client_id=key_entry.id,  # UUID — looked up by _perms.get_current_user_id
                    scopes=list(key_entry.scopes),
                )
        return None

    # ------------------------------------------------------------------ DCR

    async def get_client(
        self, client_id: str
    ) -> OAuthClientInformationFull | None:
        """Return a previously-registered client, or None if unknown."""
        return self.store.get_client(client_id)

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        """Store a freshly-registered client (called from the DCR handler).

        We **force** `client_info.scope = "*"` before storing so that
        `OAuthClientMetadata.validate_scope()` passes during the later
        /authorize call regardless of what scope claude.ai requests.
        Real authorization happens at tool level via `_perms.require()`,
        not at the OAuth scope layer, so we treat the OAuth scope field
        as "this client is allowed to request anything; the final
        enforcement is elsewhere".
        """
        client_info.scope = "*"
        logger.info(
            "oauth: registered client id=%s name=%r redirect_uris=%s",
            client_info.client_id,
            client_info.client_name or "<unnamed>",
            [str(u) for u in (client_info.redirect_uris or [])],
        )
        self.store.put_client(client_info)

    # ------------------------------------------------------------------ authorize → consent

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Start the user-consent phase of the authorization_code flow.

        FastMCP's AuthorizationHandler has already validated the client,
        the redirect_uri, and the scope. We stash the request state in
        memory under a random `pending_id`, then return a URL pointing
        at our own consent page. FastMCP will 302 the browser there,
        the user enters the admin secret, and the consent POST handler
        calls `complete_authorize` / `deny_authorize` to finish the
        flow by redirecting back to `params.redirect_uri` with a code
        (or an error).
        """
        pending_id = secrets.token_urlsafe(32)
        entry = PendingConsent(
            pending_id=pending_id,
            client=client,
            params=params,
            created_at=time.time(),
        )
        self.pending.put(entry)
        logger.info(
            "oauth: authorize pending id=%s client=%s",
            pending_id,
            client.client_id,
        )
        return f"{self.public_url}oauth/consent?pending={pending_id}"

    def complete_authorize(
        self, pending_id: str
    ) -> tuple[str, str, str | None]:
        """Consent-page helper: admin approved, mint an auth code.

        Pops the pending state, generates a single-use AuthorizationCode
        with a 60 s TTL, stores it, and returns
        `(redirect_uri, code, state)` so the consent HTTP handler can
        redirect the user's browser back to the client.

        Raises:
            ValueError: pending state not found or expired.
        """
        entry = self.pending.pop(pending_id)
        if entry is None:
            raise ValueError("pending consent not found or expired")

        client = entry.client
        params = entry.params
        code_str = f"code_{secrets.token_urlsafe(24)}"
        scopes = params.scopes if params.scopes is not None else ["*"]

        auth_code = AuthorizationCode(
            code=code_str,
            scopes=scopes,
            expires_at=time.time() + AUTH_CODE_TTL_S,
            client_id=client.client_id or "",
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self.auth_codes.put(auth_code)

        logger.info(
            "oauth: consent approved pending=%s client=%s code=%s...",
            pending_id,
            client.client_id,
            code_str[:12],
        )
        return str(params.redirect_uri), code_str, params.state

    def deny_authorize(self, pending_id: str) -> tuple[str, str | None]:
        """Consent-page helper: user clicked Deny.

        Raises:
            ValueError: pending state not found or expired.
        """
        entry = self.pending.pop(pending_id)
        if entry is None:
            raise ValueError("pending consent not found or expired")

        logger.info(
            "oauth: consent denied pending=%s client=%s",
            pending_id,
            entry.client.client_id,
        )
        return str(entry.params.redirect_uri), entry.params.state

    # ------------------------------------------------------------------ token exchange

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Look up a stored authorization code.

        FastMCP's TokenHandler calls this before doing PKCE verification
        and the redirect_uri check. We only return the code if the
        client_id on it matches the caller, to match the "pretend it
        doesn't exist if it belongs to another client" spec guidance.
        """
        code = self.auth_codes.get(authorization_code)
        if code is None:
            return None
        if code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Trade a valid authorization code for an access + refresh token.

        The code is single-use: we pop it from the store. If this method
        fires twice for the same code (race), the second call raises
        TokenError("invalid_grant"), which FastMCP translates to 400.
        """
        popped = self.auth_codes.pop(authorization_code.code)
        if popped is None:
            raise TokenError(
                "invalid_grant",
                "authorization code already used or expired",
            )

        now = int(time.time())
        access_token_str = f"tok_oauth_{secrets.token_hex(32)}"
        refresh_token_str = f"ref_oauth_{secrets.token_hex(32)}"

        access_rec = AccessTokenRecord(
            token=access_token_str,
            client_id=client.client_id or "",
            scopes=list(authorization_code.scopes),
            expires_at=now + self.access_token_ttl_s,
            resource=authorization_code.resource,
        )
        refresh_rec = RefreshTokenRecord(
            token=refresh_token_str,
            client_id=client.client_id or "",
            scopes=list(authorization_code.scopes),
            expires_at=None,
            resource=authorization_code.resource,
        )
        self.store.put_access_token(access_rec)
        self.store.put_refresh_token(refresh_rec)

        logger.info(
            "oauth: issued access+refresh client=%s expires_in=%d scopes=%s",
            client.client_id,
            self.access_token_ttl_s,
            " ".join(authorization_code.scopes) or "<none>",
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=self.access_token_ttl_s,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes) or None,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Look up a stored refresh token for a given client."""
        rec = self.store.get_refresh_token(refresh_token)
        if rec is None:
            return None
        if rec.client_id != client.client_id:
            return None
        return RefreshToken(
            token=rec.token,
            client_id=rec.client_id,
            scopes=list(rec.scopes),
            expires_at=rec.expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate: invalidate the old refresh token, issue a new pair.

        This makes every refresh exchange a complete reset: new access
        token, new refresh token, old refresh token unusable. Limits
        the blast radius of a leaked refresh token to a single refresh.
        """
        self.store.delete_refresh_token(refresh_token.token)

        now = int(time.time())
        access_token_str = f"tok_oauth_{secrets.token_hex(32)}"
        new_refresh_str = f"ref_oauth_{secrets.token_hex(32)}"

        access_rec = AccessTokenRecord(
            token=access_token_str,
            client_id=client.client_id or "",
            scopes=list(scopes),
            expires_at=now + self.access_token_ttl_s,
        )
        refresh_rec = RefreshTokenRecord(
            token=new_refresh_str,
            client_id=client.client_id or "",
            scopes=list(scopes),
            expires_at=None,
        )
        self.store.put_access_token(access_rec)
        self.store.put_refresh_token(refresh_rec)

        logger.info(
            "oauth: refreshed tokens client=%s (old refresh invalidated)",
            client.client_id,
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=self.access_token_ttl_s,
            refresh_token=new_refresh_str,
            scope=" ".join(scopes) or None,
        )

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        """Revoke an access or refresh token.

        If a refresh token is revoked we also drop all of that client's
        access tokens, effectively ending the session. Access-token-only
        revocation leaves the refresh token alone (caller can still
        refresh), which is the intended UX for short-access-token flows.
        """
        if isinstance(token, RefreshToken):
            self.store.delete_refresh_token(token.token)
            self.store.delete_access_tokens_for_client(token.client_id)
        else:
            self.store.delete_access_token(token.token)
        logger.info(
            "oauth: revoked %s client=%s",
            "refresh" if isinstance(token, RefreshToken) else "access",
            token.client_id,
        )


# -----------------------------------------------------------------------------
# Consent page — HTML renderer and Starlette route
# -----------------------------------------------------------------------------

_CONSENT_CSS = """
  :root {
    color-scheme: dark light;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    max-width: 480px;
    margin: 4em auto;
    padding: 2em;
    color: #e4e4e7;
    background: #0a0a0a;
    line-height: 1.5;
  }
  h1 { font-size: 1.4em; margin-top: 0; color: #f4f4f5; }
  p { color: #a1a1aa; }
  .info {
    background: #18181b;
    border: 1px solid #27272a;
    padding: 1em 1.2em;
    border-radius: 0.5em;
    margin: 1.5em 0;
  }
  .info dl { margin: 0; }
  .info dt { font-weight: 600; color: #d4d4d8; margin-top: 0.5em; }
  .info dt:first-child { margin-top: 0; }
  .info dd {
    margin: 0 0 0.5em 0;
    color: #a1a1aa;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 0.9em;
    word-break: break-all;
  }
  label {
    display: block;
    margin: 1.5em 0 0.5em 0;
    color: #d4d4d8;
    font-weight: 500;
  }
  input[type=password] {
    width: 100%;
    padding: 0.8em 1em;
    border: 1px solid #3f3f46;
    border-radius: 0.5em;
    background: #18181b;
    color: #f4f4f5;
    font-size: 1em;
    font-family: ui-monospace, SFMono-Regular, monospace;
    box-sizing: border-box;
  }
  input[type=password]:focus { outline: none; border-color: #6366f1; }
  .buttons {
    display: flex;
    gap: 1em;
    margin-top: 2em;
  }
  button {
    flex: 1;
    padding: 0.8em 1.5em;
    border-radius: 0.5em;
    font-size: 1em;
    font-weight: 500;
    cursor: pointer;
    border: none;
    font-family: inherit;
  }
  .btn-approve { background: #6366f1; color: white; }
  .btn-approve:hover { background: #4f46e5; }
  .btn-deny { background: #27272a; color: #d4d4d8; border: 1px solid #3f3f46; }
  .btn-deny:hover { background: #3f3f46; }
  .error {
    background: #450a0a;
    border: 1px solid #7f1d1d;
    color: #fca5a5;
    padding: 0.8em 1.2em;
    border-radius: 0.5em;
    margin: 1em 0;
    font-size: 0.9em;
  }
  @media (prefers-color-scheme: light) {
    body { background: #fafafa; color: #18181b; }
    h1 { color: #09090b; }
    p { color: #52525b; }
    .info { background: white; border-color: #e4e4e7; }
    .info dt { color: #27272a; }
    .info dd { color: #52525b; }
    label { color: #27272a; }
    input[type=password] {
      background: white;
      border-color: #d4d4d8;
      color: #09090b;
    }
    .btn-deny {
      background: #f4f4f5;
      color: #27272a;
      border-color: #d4d4d8;
    }
    .btn-deny:hover { background: #e4e4e7; }
    .error { background: #fef2f2; border-color: #fecaca; color: #991b1b; }
  }
"""


def _render_consent_html(
    *,
    client_name: str,
    client_id: str,
    scopes: list[str],
    pending_id: str,
    error: str | None = None,
) -> str:
    """Render the single-page HTML consent form.

    Inline CSS, no external assets, no JavaScript. Shown once per
    device at initial Custom Connector setup; after that, claude.ai
    holds a refresh token and the user doesn't see this page unless
    they explicitly re-authorize.
    """
    scope_str = " ".join(scopes) if scopes else "*"
    error_block = (
        f'<div class="error">{_html.escape(error)}</div>' if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mcp-brain — authorize connection</title>
  <style>{_CONSENT_CSS}</style>
</head>
<body>
  <h1>mcp-brain — authorize connection</h1>
  <p>A client wants to connect to your mcp-brain personal MCP server. Review the details below and enter your admin secret to approve.</p>
  <div class="info">
    <dl>
      <dt>Client name</dt><dd>{_html.escape(client_name or "<unnamed>")}</dd>
      <dt>Client ID</dt><dd>{_html.escape(client_id)}</dd>
      <dt>Scopes</dt><dd>{_html.escape(scope_str)}</dd>
    </dl>
  </div>
  {error_block}
  <form method="POST" action="/oauth/consent" autocomplete="off">
    <input type="hidden" name="pending" value="{_html.escape(pending_id)}">
    <label for="admin_secret">Admin secret</label>
    <input type="password" id="admin_secret" name="admin_secret" autofocus required>
    <div class="buttons">
      <button type="submit" name="action" value="authorize" class="btn-approve">Authorize</button>
      <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
    </div>
  </form>
</body>
</html>"""


def register_oauth_consent_route(
    mcp: "FastMCP",
    provider: ChainedProvider,
) -> None:
    """Register GET + POST /oauth/consent on the FastMCP instance.

    The consent page is NOT protected by bearer auth — claude.ai can't
    attach one, and that's the whole point of the page. Security comes
    from the admin_secret field in the POSTed form, which must match
    `MCP_OAUTH_ADMIN_SECRET` (checked with `hmac.compare_digest` for
    constant-time comparison).

    If the server starts without an admin secret configured, GET and
    POST both return 503 with a clear explanation.
    """
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

    @mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
    async def consent(request: Request):
        # If admin hasn't set the secret in env, fail loudly. Server
        # still boots and yaml bearer auth still works — this only
        # blocks the OAuth consent step.
        if not provider.admin_secret:
            return PlainTextResponse(
                "OAuth flow is not configured on this server. The "
                "operator must set MCP_OAUTH_ADMIN_SECRET and restart.",
                status_code=503,
            )

        if request.method == "GET":
            pending_id = request.query_params.get("pending", "")
            entry = provider.pending.get(pending_id)
            if entry is None:
                return PlainTextResponse(
                    "Consent request not found or expired. Start the "
                    "connection flow again from your client.",
                    status_code=404,
                )
            return HTMLResponse(
                _render_consent_html(
                    client_name=entry.client.client_name or "",
                    client_id=entry.client.client_id or "",
                    scopes=entry.params.scopes or ["*"],
                    pending_id=pending_id,
                )
            )

        # POST
        form = await request.form()
        pending_id = str(form.get("pending", ""))
        action = str(form.get("action", "authorize"))
        submitted_secret = str(form.get("admin_secret", ""))

        entry = provider.pending.get(pending_id)
        if entry is None:
            return PlainTextResponse(
                "Consent request not found or expired.",
                status_code=404,
            )

        if action == "deny":
            try:
                redirect_uri, state = provider.deny_authorize(pending_id)
            except ValueError as exc:
                return PlainTextResponse(str(exc), status_code=404)
            url = construct_redirect_uri(
                redirect_uri,
                error="access_denied",
                state=state,
            )
            return RedirectResponse(url=url, status_code=302)

        # action == "authorize": verify admin secret (constant-time)
        if not hmac.compare_digest(
            submitted_secret.encode("utf-8"),
            provider.admin_secret.encode("utf-8"),
        ):
            logger.warning(
                "oauth: consent rejected (wrong secret) pending=%s client=%s",
                pending_id,
                entry.client.client_id,
            )
            return HTMLResponse(
                _render_consent_html(
                    client_name=entry.client.client_name or "",
                    client_id=entry.client.client_id or "",
                    scopes=entry.params.scopes or ["*"],
                    pending_id=pending_id,
                    error="Wrong admin secret. Try again.",
                ),
                status_code=401,
            )

        # Approved: mint the auth code and redirect back to client
        try:
            redirect_uri, code, state = provider.complete_authorize(pending_id)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=404)

        url = construct_redirect_uri(redirect_uri, code=code, state=state)
        return RedirectResponse(url=url, status_code=302)
