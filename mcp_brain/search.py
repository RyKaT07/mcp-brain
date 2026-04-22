"""Full-text search index backed by SQLite FTS5.

Indexes knowledge markdown files split by H2 sections, plus Todoist tasks
and Trello cards when API keys are available. Allows BM25-ranked queries
scoped to allowed knowledge scopes, with optional source filtering.

When a ``db_path`` is provided the index is persisted to disk so it
survives worker restarts.  A fingerprint of the knowledge directory
(file paths + mtimes) is stored in a metadata table; ``build()`` skips
the rebuild when the fingerprint matches.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"_meta", "inbox", ".git", "users"}

# Bump this whenever the index schema or the parsing/extraction code
# changes in a way that makes previously-persisted rows incorrect or
# inconsistent.  On mismatch the on-disk DB is wiped and rebuilt on the
# next build() call.  Fingerprint-based skip alone cannot detect code
# changes (only file mtimes), so this counter is the escape hatch.
_SCHEMA_VERSION = "1"


def _knowledge_fingerprint(knowledge_dir: Path) -> str:
    """Compute a fast fingerprint of all knowledge markdown files.

    Uses sorted (relative path, mtime, size) tuples hashed with SHA-256.
    """
    entries: list[str] = []
    for scope_dir in sorted(knowledge_dir.iterdir()):
        if not scope_dir.is_dir() or scope_dir.name in _SKIP_DIRS:
            continue
        for md_file in sorted(scope_dir.glob("*.md")):
            try:
                st = md_file.stat()
                entries.append(f"{md_file.relative_to(knowledge_dir)}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                continue
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def _parse_sections(content: str) -> dict[str, str]:
    """Parse markdown into {section_title: section_body} by H2 headers."""
    sections: dict[str, str] = {}
    current_title = "_preamble"
    current_lines: list[str] = []

    for line in content.splitlines(keepends=True):
        if line.startswith("## "):
            sections[current_title] = "".join(current_lines)
            current_title = line.strip("# \n")
            current_lines = []
        else:
            current_lines.append(line)

    sections[current_title] = "".join(current_lines)
    return sections


class SearchIndex:
    """SQLite FTS5 index over knowledge markdown files and tasks.

    When *db_path* is provided the database is stored on disk and
    ``build()`` skips re-indexing if the knowledge directory hasn't
    changed (fingerprint match).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._db_path = db_path
        db_uri = str(db_path) if db_path else ":memory:"
        self._conn = sqlite3.connect(db_uri, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                scope, project, section, content, source,
                tokenize = "porter unicode61"
            )
            """
        )
        self._conn.commit()

        self._enforce_schema_version()

        # Per-user indexes: user_id → SearchIndex (leaf instances, not recursive).
        # Created lazily on first write or explicit build_user() call.
        self._user_indexes: dict[str, "SearchIndex"] = {}
        self._user_indexes_lock = threading.Lock()

    def _enforce_schema_version(self) -> None:
        """Wipe persisted rows if the on-disk schema version is out of date.

        The FTS table + _meta rows are cleared (so the next ``build()`` does a
        full rebuild) and the current ``_SCHEMA_VERSION`` is recorded.  For
        in-memory DBs (``db_path is None``) this is still safe and cheap — the
        tables are empty at this point anyway.
        """
        cur = self._conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'")
        row = cur.fetchone()
        stored = row[0] if row else None
        if stored == _SCHEMA_VERSION:
            return
        if stored is not None:
            logger.info(
                "search: schema version %s → %s, rebuilding index",
                stored,
                _SCHEMA_VERSION,
            )
        self._conn.execute("DELETE FROM knowledge_fts")
        self._conn.execute("DELETE FROM _meta WHERE key = 'fingerprint'")
        self._conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Per-user index management
    # ------------------------------------------------------------------

    def _get_or_create_user_index(self, user_id: str) -> "SearchIndex":
        """Return the per-user SearchIndex, creating an empty one if needed."""
        with self._user_indexes_lock:
            if user_id not in self._user_indexes:
                self._user_indexes[user_id] = SearchIndex()
            return self._user_indexes[user_id]

    def has_user_index(self, user_id: str) -> bool:
        """Return True if a per-user index has been created for *user_id*."""
        with self._user_indexes_lock:
            return user_id in self._user_indexes

    def get_user_index(self, user_id: str) -> "SearchIndex | None":
        """Return the per-user SearchIndex or None if it doesn't exist yet."""
        with self._user_indexes_lock:
            return self._user_indexes.get(user_id)

    def build_user(self, user_id: str, user_dir: Path) -> None:
        """(Re)build the per-user search index from *user_dir*."""
        idx = self._get_or_create_user_index(user_id)
        idx.build(user_dir)

    def update_file_for_user(
        self, user_id: str, scope: str, project: str, content: str
    ) -> None:
        """Re-index a single file in the per-user index."""
        idx = self._get_or_create_user_index(user_id)
        idx.update_file(scope, project, content)

    def remove_file_for_user(self, user_id: str, scope: str, project: str) -> None:
        """Remove a file from the per-user index."""
        with self._user_indexes_lock:
            idx = self._user_indexes.get(user_id)
        if idx is not None:
            idx.remove_file(scope, project)

    # ------------------------------------------------------------------
    # Build / refresh
    # ------------------------------------------------------------------

    def build(self, knowledge_dir: Path) -> None:
        """Scan all knowledge/{scope}/{project}.md files and index them.

        Only clears and re-indexes knowledge entries; task entries
        (source=todoist/trello) are preserved across rebuilds.

        When using a file-backed DB, skips the rebuild if the knowledge
        directory fingerprint hasn't changed since the last build.
        """
        fingerprint = _knowledge_fingerprint(knowledge_dir)

        # Check cached fingerprint — skip rebuild if unchanged.
        # Only meaningful for file-backed DBs; in-memory always rebuilds.
        if self._db_path is not None:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT value FROM _meta WHERE key = 'fingerprint'"
                )
                row = cur.fetchone()
                if row and row[0] == fingerprint:
                    logger.info("search.build: cache hit — skipping rebuild")
                    return

        t0 = time.monotonic()
        file_count = 0
        section_count = 0

        rows: list[tuple[str, str, str, str, str]] = []

        for scope_dir in knowledge_dir.iterdir():
            if not scope_dir.is_dir():
                continue
            if scope_dir.name in _SKIP_DIRS:
                continue
            scope = scope_dir.name

            for md_file in scope_dir.glob("*.md"):
                project = md_file.stem
                try:
                    file_content = md_file.read_text(encoding="utf-8")
                except Exception:
                    logger.warning("search.build: could not read %s", md_file)
                    continue

                sections = _parse_sections(file_content)
                for section_title, section_body in sections.items():
                    combined = f"{section_title}\n{section_body}".strip()
                    if not combined:
                        continue
                    rows.append((scope, project, section_title, combined, "knowledge"))
                    section_count += 1

                file_count += 1

        with self._lock:
            self._conn.execute("DELETE FROM knowledge_fts WHERE source = 'knowledge'")
            if rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content, source) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta(key, value) VALUES ('fingerprint', ?)",
                (fingerprint,),
            )
            self._conn.commit()

        elapsed = time.monotonic() - t0
        logger.info(
            "search.build: indexed %d files, %d sections in %.3fs",
            file_count,
            section_count,
            elapsed,
        )

    def update_file(self, scope: str, project: str, content: str) -> None:
        """Re-index a single knowledge file (delete old rows, insert fresh ones)."""
        sections = _parse_sections(content)
        rows: list[tuple[str, str, str, str, str]] = []
        for section_title, section_body in sections.items():
            combined = f"{section_title}\n{section_body}".strip()
            if not combined:
                continue
            rows.append((scope, project, section_title, combined, "knowledge"))

        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_fts WHERE scope = ? AND project = ? AND source = 'knowledge'",
                (scope, project),
            )
            if rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content, source) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.commit()

    def remove_file(self, scope: str, project: str) -> None:
        """Remove all knowledge index rows for a scope/project."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_fts WHERE scope = ? AND project = ? AND source = 'knowledge'",
                (scope, project),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Task indexing
    # ------------------------------------------------------------------

    def index_todoist_tasks(self, tasks: list[dict], scope: str = "user") -> int:
        """Index Todoist tasks into FTS. Returns count indexed.

        Each task dict must have:
          - content: task title string
          - project_name: Todoist project name (used as FTS project column)
          - section_name: Todoist section name (used as FTS section column, may be empty)
        """
        rows: list[tuple[str, str, str, str, str]] = []
        for t in tasks:
            content = t.get("content", "").strip()
            if not content:
                continue
            project = t.get("project_name", "Inbox")
            section = t.get("section_name", "") or ""
            rows.append((scope, project, section, content, "todoist"))

        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_fts WHERE source = 'todoist' AND scope = ?",
                (scope,),
            )
            if rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content, source) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.commit()

        logger.info(
            "search.index_todoist_tasks: indexed %d tasks for scope=%s",
            len(rows),
            scope,
        )
        return len(rows)

    def index_trello_cards(self, cards: list[dict], scope: str = "user") -> int:
        """Index Trello cards into FTS. Returns count indexed.

        Each card dict must have:
          - name: card title string
          - board_name: Trello board name (used as FTS project column)
          - list_name: Trello list name (used as FTS section column)
        """
        rows: list[tuple[str, str, str, str, str]] = []
        for c in cards:
            name = c.get("name", "").strip()
            if not name:
                continue
            board_name = c.get("board_name", "")
            list_name = c.get("list_name", "")
            rows.append((scope, board_name, list_name, name, "trello"))

        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_fts WHERE source = 'trello' AND scope = ?",
                (scope,),
            )
            if rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content, source) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.commit()

        logger.info(
            "search.index_trello_cards: indexed %d cards for scope=%s",
            len(rows),
            scope,
        )
        return len(rows)

    def refresh_tasks(
        self,
        todoist_tasks: list[dict] | None = None,
        trello_cards: list[dict] | None = None,
        scope: str = "user",
    ) -> None:
        """Atomically clear task entries for scope and optionally re-index.

        Clears all todoist and trello entries for the given scope in one
        transaction before inserting fresh data.
        """
        todoist_rows: list[tuple[str, str, str, str, str]] = []
        if todoist_tasks is not None:
            for t in todoist_tasks:
                content = t.get("content", "").strip()
                if content:
                    todoist_rows.append(
                        (scope, t.get("project_name", "Inbox"), t.get("section_name", "") or "", content, "todoist")
                    )

        trello_rows: list[tuple[str, str, str, str, str]] = []
        if trello_cards is not None:
            for c in trello_cards:
                name = c.get("name", "").strip()
                if name:
                    trello_rows.append(
                        (scope, c.get("board_name", ""), c.get("list_name", ""), name, "trello")
                    )

        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_fts WHERE source IN ('todoist', 'trello') AND scope = ?",
                (scope,),
            )
            if todoist_rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content, source) VALUES (?, ?, ?, ?, ?)",
                    todoist_rows,
                )
            if trello_rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content, source) VALUES (?, ?, ?, ?, ?)",
                    trello_rows,
                )
            self._conn.commit()

        logger.info(
            "search.refresh_tasks: refreshed %d todoist + %d trello entries for scope=%s",
            len(todoist_rows),
            len(trello_rows),
            scope,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        allowed_scopes: list[str] | None,
        limit: int = 10,
        source: str | None = None,
    ) -> list[dict]:
        """Run a BM25-ranked FTS5 query and return results.

        Returns a list of dicts: {scope, project, section, snippet, rank, source}.

        Args:
            query: Full-text search query string.
            allowed_scopes: If set, restrict results to these scopes. None = all.
            limit: Maximum results to return.
            source: If set, restrict results to this source ("knowledge",
                    "todoist", or "trello"). None = all sources.
        """
        if not query or not query.strip():
            return []

        conditions: list[str] = ["knowledge_fts MATCH ?"]
        params: list = [query]

        if allowed_scopes is not None:
            placeholders = ",".join("?" * len(allowed_scopes))
            conditions.append(f"scope IN ({placeholders})")
            params.extend(allowed_scopes)

        if source is not None:
            conditions.append("source = ?")
            params.append(source)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT
                scope,
                project,
                section,
                snippet(knowledge_fts, 3, '<b>', '</b>', '…', 16) AS snippet,
                bm25(knowledge_fts) AS rank,
                source
            FROM knowledge_fts
            WHERE {where}
            ORDER BY rank
            LIMIT ?
        """
        params.append(limit)

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                rows = cursor.fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning("search.search: query error: %s", exc)
                return []

        return [
            {
                "scope": row[0],
                "project": row[1],
                "section": row[2],
                "snippet": row[3],
                "rank": row[4],
                "source": row[5],
            }
            for row in rows
        ]
