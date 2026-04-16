"""Full-text search index backed by SQLite FTS5 (in-memory).

Indexes knowledge markdown files split by H2 sections, allowing BM25-ranked
queries scoped to allowed knowledge scopes.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"_meta", "inbox", ".git", "users"}


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
    """In-memory SQLite FTS5 index over knowledge markdown files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute(
            """
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                scope, project, section, content,
                tokenize = "porter unicode61"
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Build / refresh
    # ------------------------------------------------------------------

    def build(self, knowledge_dir: Path) -> None:
        """Scan all knowledge/{scope}/{project}.md files and index them."""
        t0 = time.monotonic()
        file_count = 0
        section_count = 0

        rows: list[tuple[str, str, str, str]] = []

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
                    rows.append((scope, project, section_title, combined))
                    section_count += 1

                file_count += 1

        with self._lock:
            self._conn.execute("DELETE FROM knowledge_fts")
            if rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content) VALUES (?, ?, ?, ?)",
                    rows,
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
        """Re-index a single file (delete old rows, insert fresh ones)."""
        sections = _parse_sections(content)
        rows: list[tuple[str, str, str, str]] = []
        for section_title, section_body in sections.items():
            combined = f"{section_title}\n{section_body}".strip()
            if not combined:
                continue
            rows.append((scope, project, section_title, combined))

        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_fts WHERE scope = ? AND project = ?",
                (scope, project),
            )
            if rows:
                self._conn.executemany(
                    "INSERT INTO knowledge_fts(scope, project, section, content) VALUES (?, ?, ?, ?)",
                    rows,
                )
            self._conn.commit()

    def remove_file(self, scope: str, project: str) -> None:
        """Remove all index rows for a scope/project."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_fts WHERE scope = ? AND project = ?",
                (scope, project),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        allowed_scopes: list[str] | None,
        limit: int = 10,
    ) -> list[dict]:
        """Run a BM25-ranked FTS5 query and return results.

        Returns a list of dicts: {scope, project, section, snippet, rank}.
        """
        if not query or not query.strip():
            return []

        if allowed_scopes is not None:
            placeholders = ",".join("?" * len(allowed_scopes))
            sql = f"""
                SELECT
                    scope,
                    project,
                    section,
                    snippet(knowledge_fts, 3, '<b>', '</b>', '…', 16) AS snippet,
                    bm25(knowledge_fts) AS rank
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ?
                  AND scope IN ({placeholders})
                ORDER BY rank
                LIMIT ?
            """
            params: list = [query, *allowed_scopes, limit]
        else:
            sql = """
                SELECT
                    scope,
                    project,
                    section,
                    snippet(knowledge_fts, 3, '<b>', '</b>', '…', 16) AS snippet,
                    bm25(knowledge_fts) AS rank
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            params = [query, limit]

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
            }
            for row in rows
        ]
