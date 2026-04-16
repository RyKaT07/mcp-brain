"""
Integration credential store — JSON-backed, per-integration isolation.

Persists integration credentials to /data/integrations.json. Follows the
same pattern as KeyStore (keystore.py): atomic writes, thread-safe, never
returns secret values in list operations.

Supported integrations are listed in KNOWN_INTEGRATIONS. The store validates
that all required keys are present before saving. Secret values are stored in
plaintext (the file is chmod 600) but never included in list_configured()
output — only key names and configured status are returned.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

KNOWN_INTEGRATIONS: dict[str, list[str]] = {
    "todoist": ["TODOIST_API_KEY"],
    "trello": ["TRELLO_API_KEY", "TRELLO_API_TOKEN"],
    "nextcloud": ["NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_PASSWORD"],
    "gcal": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
}


class IntegrationStore:
    """JSON-backed store for integration credentials.

    Usage::

        store = IntegrationStore(Path("data/integrations.json"))
        store.set("todoist", {"TODOIST_API_KEY": "tok_xxx"})
        creds = store.get("todoist")   # {"TODOIST_API_KEY": "tok_xxx"}
        store.delete("todoist")
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = Lock()
        self._integrations: dict[str, dict[str, str]] = self._load()

    # ------------------------------------------------------------------
    # Persistence

    def _load(self) -> dict[str, dict[str, str]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected a JSON object")
            return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except Exception as exc:
            logger.error(
                "integration store at %s is unreadable (%s); starting empty",
                self._path,
                exc,
            )
            return {}

    def _save(self) -> None:
        """Atomic write: write to .tmp then os.replace. chmod 600."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = json.dumps(self._integrations, indent=2)
        tmp.write_text(data, encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Mutations

    def set(self, name: str, credentials: dict[str, str]) -> None:
        """Validate and persist credentials for a named integration.

        Raises ValueError if the integration name is unknown or if the
        credentials dict is missing required keys.
        """
        if name not in KNOWN_INTEGRATIONS:
            raise ValueError(f"Unknown integration: '{name}'")
        required = set(KNOWN_INTEGRATIONS[name])
        provided = set(credentials.keys())
        missing = required - provided
        if missing:
            raise ValueError(
                f"Missing required keys for '{name}': {sorted(missing)}"
            )
        # Only store the expected keys — drop any extras.
        stored = {k: credentials[k] for k in required}
        with self._lock:
            self._integrations[name] = stored
            self._save()
        logger.info("integration_store: configured '%s'", name)

    def delete(self, name: str) -> bool:
        """Remove a named integration. Returns True if it was present."""
        with self._lock:
            if name not in self._integrations:
                return False
            del self._integrations[name]
            self._save()
        logger.info("integration_store: removed '%s'", name)
        return True

    # ------------------------------------------------------------------
    # Lookups

    def get(self, name: str) -> dict[str, str] | None:
        """Return stored credentials for a named integration, or None."""
        return self._integrations.get(name)

    def list_configured(self) -> list[dict]:
        """Return status of ALL known integrations (never exposes secret values).

        Returns a list of dicts with:
          - name: integration identifier
          - configured: True if credentials are stored
          - keys: list of required key names (not values)
        """
        result = []
        for name, required_keys in KNOWN_INTEGRATIONS.items():
            result.append(
                {
                    "name": name,
                    "configured": name in self._integrations,
                    "keys": required_keys,
                }
            )
        return result
