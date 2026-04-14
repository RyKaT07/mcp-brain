"""
Dynamic API key store — JSON-backed, per-user isolation.

Tokens generated here are the "dynamic" counterpart to the static
yaml-configured bearers in `config/auth.yaml`. Each key belongs to a
`user_id` (arbitrary opaque string — email, UUID, handle) and carries
its own scope list, so per-user access control works the same way as
the existing yaml tokens.

Persistence
-----------
Keys are stored in `data/keys.json` (atomic write via .tmp + os.replace).
Revocation sets `revoked_at` rather than deleting the record so usage
history survives after a key is disabled.

Thread safety
-------------
All mutations are protected by a `threading.Lock`. The server is
single-process; the lock is cheap and prevents double-writes under
high concurrency.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class KeyEntry(BaseModel):
    """One dynamically generated API key."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    token: str
    user_id: str
    description: str | None = None
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class KeyStore:
    """JSON-backed store for dynamically generated API keys.

    Usage::

        store = KeyStore(Path("data/keys.json"))
        entry = store.generate("alice@example.com", ["knowledge:read:*"])
        print(entry.token)   # tok_<64 hex chars> — show once, save it
        store.revoke(entry.id)
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = Lock()
        self._keys: list[KeyEntry] = self._load()

    # ------------------------------------------------------------------
    # Persistence

    def _load(self) -> list[KeyEntry]:
        if not self._path.exists():
            return []
        try:
            raw: dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
            return [KeyEntry.model_validate(k) for k in raw.get("keys", [])]
        except Exception as exc:
            logger.error(
                "keystore at %s is unreadable (%s); starting empty — "
                "existing dynamic keys will not be accepted",
                self._path,
                exc,
            )
            return []

    def _save(self) -> None:
        """Atomic write: write to .tmp then os.replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = json.dumps(
            {"keys": [k.model_dump(mode="json") for k in self._keys]},
            indent=2,
            default=str,
        )
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Mutations

    def generate(
        self,
        user_id: str,
        scopes: list[str],
        description: str | None = None,
    ) -> KeyEntry:
        """Generate a new API key for `user_id` and persist it.

        The returned `KeyEntry.token` is the secret — callers must show
        it to the user exactly once. It is not stored in plaintext
        anywhere that can be re-retrieved later.
        """
        entry = KeyEntry(
            token="tok_" + secrets.token_hex(32),
            user_id=user_id,
            description=description,
            scopes=scopes,
        )
        with self._lock:
            self._keys.append(entry)
            self._save()
        logger.info("keystore: generated key %s for user %s", entry.id, user_id)
        return entry

    def revoke(self, key_id: str) -> bool:
        """Revoke a key by ID. Returns True if found and revoked."""
        with self._lock:
            for k in self._keys:
                if k.id == key_id and k.is_active:
                    k.revoked_at = datetime.now(timezone.utc)
                    self._save()
                    logger.info("keystore: revoked key %s", key_id)
                    return True
        return False

    # ------------------------------------------------------------------
    # Lookups

    def by_token(self, token: str) -> KeyEntry | None:
        """Return an active key matching `token`, or None."""
        for k in self._keys:
            if k.token == token and k.is_active:
                return k
        return None

    def by_id(self, key_id: str) -> KeyEntry | None:
        """Return a key by UUID (active or revoked), or None."""
        for k in self._keys:
            if k.id == key_id:
                return k
        return None

    def list_keys(
        self,
        user_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[KeyEntry]:
        """Return keys, optionally filtered by user and revocation status."""
        return [
            k
            for k in self._keys
            if (include_revoked or k.is_active)
            and (user_id is None or k.user_id == user_id)
        ]
