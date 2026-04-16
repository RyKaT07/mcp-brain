"""
Usage metering — per-key call counters for future billing.

Each tool call that goes through `meter_call()` is counted against the
active token's key ID. Data is persisted to `data/usage.json` after
every write so a restart never loses counts.

Schema (usage.json)
-------------------
::

    {
      "calls": {
        "<key_id>": {
          "total": 42,
          "by_tool": {"knowledge_read": 30, "knowledge_update": 12},
          "last_at": "2026-04-14T18:00:00+00:00"
        }
      }
    }

Thread safety
-------------
All reads and writes are protected by a Lock. Single-process server —
the lock prevents double-increments under concurrent requests.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class UsageMeter:
    """JSON-backed per-key call counter.

    Usage::

        meter = UsageMeter(Path("data/usage.json"))
        meter.record("key-uuid-here", "knowledge_read")
        stats = meter.stats()   # all keys
        stats = meter.stats("key-uuid-here")   # one key
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = Lock()
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence

    def _load(self) -> dict:
        if not self._path.exists():
            return {"calls": {}}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(
                "usage store at %s is unreadable (%s); starting empty",
                self._path,
                exc,
            )
            return {"calls": {}}

    def _save(self) -> None:
        """Atomic write: write to .tmp then os.replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Mutations

    def record(self, key_id: str, tool: str) -> None:
        """Increment the call counter for `key_id` / `tool`.

        Writes through to disk on every call. Counts are small and
        infrequent enough that the I/O cost is acceptable; deferring
        writes would risk losing data on crash.
        """
        with self._lock:
            calls: dict = self._data.setdefault("calls", {})
            entry: dict = calls.setdefault(
                key_id, {"total": 0, "by_tool": {}, "last_at": None}
            )
            entry["total"] += 1
            entry["by_tool"][tool] = entry["by_tool"].get(tool, 0) + 1
            entry["last_at"] = datetime.now(timezone.utc).isoformat()
            self._save()

    # ------------------------------------------------------------------
    # Reads

    def stats(self, key_id: str | None = None) -> dict:
        """Return usage stats.

        If `key_id` is given, return stats for that key only (empty dict
        if no calls recorded yet). Otherwise return the full calls dict.
        """
        with self._lock:
            calls: dict = self._data.get("calls", {})
            if key_id is not None:
                return dict(calls.get(key_id, {}))
            return dict(calls)
