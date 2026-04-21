"""Tests for the schema_version enforcement in SearchIndex and RelationshipGraph.

The fingerprint-based rebuild skip in ``build()`` cannot detect code changes
(only file mtimes).  ``_SCHEMA_VERSION`` is an explicit escape hatch: when it
is bumped, the on-disk DB is wiped on next open so a clean rebuild happens.

These tests assert that:
- A fresh DB records the current schema version.
- An existing DB with a matching version is left alone.
- An existing DB with an older/mismatching version has its rows cleared and the
  fingerprint removed (so the next build does a full rebuild).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# SearchIndex


class TestSearchSchemaVersion:
    def test_fresh_db_records_schema_version(self, tmp_path: Path):
        from mcp_brain.search import _SCHEMA_VERSION, SearchIndex

        db = tmp_path / "search.db"
        idx = SearchIndex(db_path=db)

        cur = idx._conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == _SCHEMA_VERSION

    def test_matching_version_preserves_rows(self, tmp_path: Path):
        from mcp_brain.search import SearchIndex

        db = tmp_path / "search.db"
        idx = SearchIndex(db_path=db)
        idx.update_file("school", "notes", "## Sec\n\nbody\n")

        # Sanity: row is present.
        row_count_before = idx._conn.execute(
            "SELECT COUNT(*) FROM knowledge_fts"
        ).fetchone()[0]
        assert row_count_before > 0
        idx._conn.close()

        # Reopen with the same version — rows should survive.
        idx2 = SearchIndex(db_path=db)
        row_count_after = idx2._conn.execute(
            "SELECT COUNT(*) FROM knowledge_fts"
        ).fetchone()[0]
        assert row_count_after == row_count_before

    def test_mismatched_version_wipes_rows(self, tmp_path: Path):
        from mcp_brain.search import SearchIndex

        db = tmp_path / "search.db"
        idx = SearchIndex(db_path=db)
        idx.update_file("school", "notes", "## Sec\n\nbody\n")
        assert idx._conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] > 0

        # Manually downgrade the recorded schema_version so the next open
        # considers the DB stale.
        idx._conn.execute(
            "UPDATE _meta SET value = ? WHERE key = 'schema_version'",
            ("legacy",),
        )
        idx._conn.commit()
        idx._conn.close()

        idx2 = SearchIndex(db_path=db)
        # Rows should have been wiped.
        assert (
            idx2._conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 0
        )
        # Fingerprint row cleared so next build() doesn't short-circuit.
        fp = idx2._conn.execute(
            "SELECT value FROM _meta WHERE key = 'fingerprint'"
        ).fetchone()
        assert fp is None

    def test_missing_version_is_treated_as_mismatch(self, tmp_path: Path):
        """Older DBs that pre-date the schema_version feature should be rebuilt.

        Pre-existing on-disk indexes do not have a schema_version row.  On first
        open under the new code they should be wiped and re-recorded.
        """
        from mcp_brain.search import _SCHEMA_VERSION

        db = tmp_path / "search.db"

        # Build a legacy DB by hand — just the FTS + _meta tables, no
        # schema_version row, with a row of indexed content.
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                scope, project, section, content, source,
                tokenize = "porter unicode61"
            )
            """
        )
        conn.execute(
            "INSERT INTO knowledge_fts(scope, project, section, content, source) "
            "VALUES ('school', 'notes', 'Sec', 'body', 'knowledge')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES ('fingerprint', 'deadbeef')"
        )
        conn.commit()
        conn.close()

        from mcp_brain.search import SearchIndex

        idx = SearchIndex(db_path=db)
        # Legacy rows wiped.
        assert idx._conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 0
        # Fingerprint cleared.
        assert (
            idx._conn.execute(
                "SELECT value FROM _meta WHERE key = 'fingerprint'"
            ).fetchone()
            is None
        )
        # Version recorded.
        assert (
            idx._conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == _SCHEMA_VERSION
        )


# ---------------------------------------------------------------------------
# RelationshipGraph


class TestGraphSchemaVersion:
    def test_fresh_db_records_schema_version(self, tmp_path: Path):
        from mcp_brain.graph import _SCHEMA_VERSION, RelationshipGraph

        db = tmp_path / "graph.db"
        g = RelationshipGraph(db_path=db)
        row = g._conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == _SCHEMA_VERSION

    def test_matching_version_preserves_rows(self, tmp_path: Path):
        from mcp_brain.graph import RelationshipGraph

        db = tmp_path / "graph.db"
        g = RelationshipGraph(db_path=db)
        g.update_file("school", "notes", "## Sec\n\n[[alice]] met [[bob]]\n")
        ent_before = g._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        assert ent_before > 0
        g._conn.close()

        g2 = RelationshipGraph(db_path=db)
        ent_after = g2._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        assert ent_after == ent_before

    def test_mismatched_version_wipes_rows(self, tmp_path: Path):
        from mcp_brain.graph import RelationshipGraph

        db = tmp_path / "graph.db"
        g = RelationshipGraph(db_path=db)
        g.update_file("school", "notes", "## Sec\n\n[[alice]] met [[bob]]\n")
        assert g._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] > 0

        g._conn.execute(
            "UPDATE _meta SET value = ? WHERE key = 'schema_version'",
            ("legacy",),
        )
        g._conn.commit()
        g._conn.close()

        g2 = RelationshipGraph(db_path=db)
        # Both tables are cleared on version mismatch.
        assert g2._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert g2._conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 0
        # Fingerprint cleared.
        assert (
            g2._conn.execute(
                "SELECT value FROM _meta WHERE key = 'fingerprint'"
            ).fetchone()
            is None
        )
