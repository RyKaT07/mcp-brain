"""Relationship graph backed by SQLite (in-memory).

Extracts entity relationships from knowledge markdown files and stores
them in an in-memory SQLite database for graph traversal queries.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"_meta", "inbox", ".git", "users"}

# Regex patterns for entity extraction
_RE_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_RE_BACKLINK = re.compile(r"@([\w/-]+)")
_RE_FILE_REF = re.compile(r"\b([\w-]+/[\w-]+)\.md\b")
_RE_H2 = re.compile(r"^## (.+)$", re.MULTILINE)


def _normalize(name: str) -> str:
    """Normalize entity name: lowercase, strip whitespace, collapse spaces."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _extract_entities_and_rels(
    scope: str,
    project: str,
    content: str,
) -> tuple[list[dict], list[dict]]:
    """Extract entities and relationships from markdown content.

    Returns (entities, relationships) where:
    - entities: list of {name, entity_type, scope, project}
    - relationships: list of {subject, predicate, object, source_scope, source_project,
                               source_section, confidence}
    """
    entities: list[dict] = []
    relationships: list[dict] = []

    # Track current H2 section for source_section metadata
    current_section = "_preamble"
    lines = content.splitlines()

    for line in lines:
        h2_match = _RE_H2.match(line)
        if h2_match:
            current_section = h2_match.group(1).strip()
            # H2 section headers become entities (confidence 0.5)
            section_name = _normalize(current_section)
            if section_name:
                entities.append(
                    {
                        "name": section_name,
                        "entity_type": "section",
                        "scope": scope,
                        "project": project,
                    }
                )
                relationships.append(
                    {
                        "subject": _normalize(f"{scope}/{project}"),
                        "predicate": "has_section",
                        "object": section_name,
                        "source_scope": scope,
                        "source_project": project,
                        "source_section": current_section,
                        "confidence": 0.5,
                    }
                )

        # [[entity]] wikilinks → mentions predicate (confidence 1.0)
        # Concept entities are global (no scope/project) so cross-file traversal works.
        for match in _RE_WIKILINK.finditer(line):
            target = _normalize(match.group(1))
            if target:
                entities.append(
                    {
                        "name": target,
                        "entity_type": "concept",
                        "scope": "",
                        "project": "",
                    }
                )
                relationships.append(
                    {
                        "subject": _normalize(f"{scope}/{project}"),
                        "predicate": "mentions",
                        "object": target,
                        "source_scope": scope,
                        "source_project": project,
                        "source_section": current_section,
                        "confidence": 1.0,
                    }
                )

        # @scope/project or @entity backlinks → references predicate (confidence 1.0)
        # If the backlink looks like scope/project, use those as entity scope/project
        # so the entity matches the actual file entity when that file is indexed.
        for match in _RE_BACKLINK.finditer(line):
            target = _normalize(match.group(1))
            if not target:
                continue
            parts = target.split("/", 1)
            if len(parts) == 2:
                tgt_scope, tgt_project = parts
            else:
                tgt_scope, tgt_project = "", ""
            entities.append(
                {
                    "name": target,
                    "entity_type": "reference",
                    "scope": tgt_scope,
                    "project": tgt_project,
                }
            )
            relationships.append(
                {
                    "subject": _normalize(f"{scope}/{project}"),
                    "predicate": "references",
                    "object": target,
                    "source_scope": scope,
                    "source_project": project,
                    "source_section": current_section,
                    "confidence": 1.0,
                }
            )

        # File path references (e.g. scope/project.md) → references predicate (confidence 0.8)
        # Use the target file's scope/project so this entity is the same node
        # as the file entity created when that file is indexed.
        for match in _RE_FILE_REF.finditer(line):
            target = _normalize(match.group(1))
            if not target:
                continue
            parts = target.split("/", 1)
            if len(parts) == 2:
                tgt_scope, tgt_project = parts
            else:
                tgt_scope, tgt_project = "", ""
            entities.append(
                {
                    "name": target,
                    "entity_type": "file",
                    "scope": tgt_scope,
                    "project": tgt_project,
                }
            )
            relationships.append(
                {
                    "subject": _normalize(f"{scope}/{project}"),
                    "predicate": "references",
                    "object": target,
                    "source_scope": scope,
                    "source_project": project,
                    "source_section": current_section,
                    "confidence": 0.8,
                }
            )

    # The file itself is an entity
    file_entity = _normalize(f"{scope}/{project}")
    entities.insert(
        0,
        {
            "name": file_entity,
            "entity_type": "file",
            "scope": scope,
            "project": project,
        },
    )

    return entities, relationships


class RelationshipGraph:
    """In-memory SQLite relationship graph over knowledge markdown files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

        # Per-user graphs: user_id → RelationshipGraph (leaf instances, not recursive).
        # Created lazily on first write or explicit build_user() call.
        self._user_graphs: dict[str, "RelationshipGraph"] = {}
        self._user_graphs_lock = threading.Lock()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'concept',
                scope   TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '',
                UNIQUE (name, scope, project)
            );

            CREATE TABLE IF NOT EXISTS relationships (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id     INTEGER NOT NULL REFERENCES entities(id),
                predicate      TEXT NOT NULL,
                object_id      INTEGER NOT NULL REFERENCES entities(id),
                source_scope   TEXT NOT NULL DEFAULT '',
                source_project TEXT NOT NULL DEFAULT '',
                source_section TEXT NOT NULL DEFAULT '',
                confidence     REAL NOT NULL DEFAULT 1.0,
                valid_from     TEXT DEFAULT NULL,
                valid_to       TEXT DEFAULT NULL,
                observed_at    TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (subject_id, predicate, object_id, source_scope, source_project)
            );

            CREATE INDEX IF NOT EXISTS idx_rel_subject  ON relationships(subject_id);
            CREATE INDEX IF NOT EXISTS idx_rel_object   ON relationships(object_id);
            CREATE INDEX IF NOT EXISTS idx_rel_pred     ON relationships(predicate);
            CREATE INDEX IF NOT EXISTS idx_rel_temporal ON relationships(valid_from, valid_to);
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_entity(self, name: str, entity_type: str, scope: str, project: str) -> int:
        """Insert entity if not exists, return its id."""
        cur = self._conn.execute(
            """
            INSERT INTO entities(name, entity_type, scope, project)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name, scope, project) DO UPDATE SET entity_type = excluded.entity_type
            RETURNING id
            """,
            (name, entity_type, scope, project),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        # Fallback: fetch existing id
        cur = self._conn.execute(
            "SELECT id FROM entities WHERE name = ? AND scope = ? AND project = ?",
            (name, scope, project),
        )
        return cur.fetchone()[0]

    def _index_file(
        self, scope: str, project: str, content: str, observed_at: str | None = None
    ) -> None:
        """Extract and insert entities/relationships for one file (lock must be held)."""
        entities, relationships = _extract_entities_and_rels(scope, project, content)

        # Upsert all entities and collect name→id map
        entity_ids: dict[str, int] = {}
        for ent in entities:
            eid = self._upsert_entity(
                ent["name"], ent["entity_type"], ent["scope"], ent["project"]
            )
            entity_ids[ent["name"]] = eid

        # Insert relationships
        for rel in relationships:
            subj = rel["subject"]
            obj = rel["object"]
            # Ensure both subject and object entities exist (object may be external)
            if subj not in entity_ids:
                eid = self._upsert_entity(subj, "file", scope, project)
                entity_ids[subj] = eid
            if obj not in entity_ids:
                # Object entity wasn't in this file's extract list — it's a
                # dangling reference. Store as a global concept so it can be
                # resolved when its source file is indexed.
                eid = self._upsert_entity(obj, "concept", "", "")
                entity_ids[obj] = eid

            if observed_at is not None:
                self._conn.execute(
                    """
                    INSERT INTO relationships
                        (subject_id, predicate, object_id, source_scope, source_project,
                         source_section, confidence, observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subject_id, predicate, object_id, source_scope, source_project)
                    DO UPDATE SET confidence = excluded.confidence,
                                  source_section = excluded.source_section
                    """,
                    (
                        entity_ids[subj],
                        rel["predicate"],
                        entity_ids[obj],
                        rel["source_scope"],
                        rel["source_project"],
                        rel["source_section"],
                        rel["confidence"],
                        observed_at,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO relationships
                        (subject_id, predicate, object_id, source_scope, source_project,
                         source_section, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subject_id, predicate, object_id, source_scope, source_project)
                    DO UPDATE SET confidence = excluded.confidence,
                                  source_section = excluded.source_section
                    """,
                    (
                        entity_ids[subj],
                        rel["predicate"],
                        entity_ids[obj],
                        rel["source_scope"],
                        rel["source_project"],
                        rel["source_section"],
                        rel["confidence"],
                    ),
                )

    # ------------------------------------------------------------------
    # Per-user graph management
    # ------------------------------------------------------------------

    def _get_or_create_user_graph(self, user_id: str) -> "RelationshipGraph":
        """Return the per-user RelationshipGraph, creating an empty one if needed."""
        with self._user_graphs_lock:
            if user_id not in self._user_graphs:
                self._user_graphs[user_id] = RelationshipGraph()
            return self._user_graphs[user_id]

    def has_user_graph(self, user_id: str) -> bool:
        """Return True if a per-user graph has been created for *user_id*."""
        with self._user_graphs_lock:
            return user_id in self._user_graphs

    def get_user_graph(self, user_id: str) -> "RelationshipGraph | None":
        """Return the per-user RelationshipGraph or None if it doesn't exist yet."""
        with self._user_graphs_lock:
            return self._user_graphs.get(user_id)

    def build_user(self, user_id: str, user_dir: Path) -> None:
        """(Re)build the per-user graph from *user_dir*."""
        graph = self._get_or_create_user_graph(user_id)
        graph.build(user_dir)

    def update_file_for_user(
        self,
        user_id: str,
        scope: str,
        project: str,
        content: str,
        observed_at: str | None = None,
    ) -> None:
        """Re-index a single file in the per-user graph."""
        graph = self._get_or_create_user_graph(user_id)
        graph.update_file(scope, project, content, observed_at=observed_at)

    def remove_file_for_user(self, user_id: str, scope: str, project: str) -> None:
        """Remove a file from the per-user graph."""
        with self._user_graphs_lock:
            graph = self._user_graphs.get(user_id)
        if graph is not None:
            graph.remove_file(scope, project)

    # ------------------------------------------------------------------
    # Build / refresh
    # ------------------------------------------------------------------

    @staticmethod
    def _git_file_timestamps(knowledge_dir: Path, md_files: list[Path]) -> dict[Path, str]:
        """Return {file_path: ISO 8601 timestamp} for all files in one git call."""
        if not md_files:
            return {}
        try:
            # Single git log call: for each file, output the path and its last commit date.
            # --name-only + --format gives us commit date followed by the filename.
            result = subprocess.run(
                ["git", "log", "--all", "--format=%aI", "--name-only", "--diff-filter=ACDMR", "--"]
                + [str(f) for f in md_files],
                cwd=knowledge_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # Parse output: alternating timestamp lines and filename lines.
            # We only need the FIRST (most recent) timestamp per file.
            timestamps: dict[Path, str] = {}
            current_ts: str | None = None
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # ISO 8601 timestamps start with a digit (year)
                if line[0].isdigit() and "T" in line:
                    current_ts = line
                elif current_ts is not None:
                    fpath = knowledge_dir / line
                    if fpath not in timestamps:
                        timestamps[fpath] = current_ts
            return timestamps
        except Exception:
            logger.warning("graph.build: batch git log failed, timestamps unavailable")
            return {}

    def build(self, knowledge_dir: Path) -> None:
        """Scan all knowledge/{scope}/{project}.md files and build the graph."""
        t0 = time.monotonic()
        file_count = 0

        # Collect all files first, then batch the git timestamp lookup.
        file_entries: list[tuple[str, str, str, Path]] = []

        for scope_dir in knowledge_dir.iterdir():
            if not scope_dir.is_dir():
                continue
            if scope_dir.name in _SKIP_DIRS:
                continue
            scope = scope_dir.name

            for md_file in scope_dir.glob("*.md"):
                project = md_file.stem
                try:
                    content = md_file.read_text(encoding="utf-8")
                except Exception:
                    logger.warning("graph.build: could not read %s", md_file)
                    continue
                file_entries.append((scope, project, content, md_file))
                file_count += 1

        # One git call for all files instead of N subprocess calls.
        timestamps = self._git_file_timestamps(
            knowledge_dir, [entry[3] for entry in file_entries]
        )

        file_data: list[tuple[str, str, str, str | None]] = [
            (scope, project, content, timestamps.get(md_file))
            for scope, project, content, md_file in file_entries
        ]

        with self._lock:
            self._conn.execute("DELETE FROM relationships")
            self._conn.execute("DELETE FROM entities")
            for scope, project, content, observed_at in file_data:
                self._index_file(scope, project, content, observed_at=observed_at)
            self._conn.commit()

        elapsed = time.monotonic() - t0
        logger.info(
            "graph.build: indexed %d files in %.3fs",
            file_count,
            elapsed,
        )

    def update_file(
        self, scope: str, project: str, content: str, observed_at: str | None = None
    ) -> None:
        """Re-index a single file (removes old relationships for that file, inserts fresh ones)."""
        with self._lock:
            # Remove old relationships originating from this file
            self._conn.execute(
                "DELETE FROM relationships WHERE source_scope = ? AND source_project = ?",
                (scope, project),
            )
            # Remove entities owned by this file (entity_type = 'file' for the file itself;
            # keep shared/external entities that other files may reference)
            self._conn.execute(
                "DELETE FROM entities WHERE scope = ? AND project = ? AND entity_type = 'file' AND name = ?",
                (scope, project, _normalize(f"{scope}/{project}")),
            )
            self._index_file(scope, project, content, observed_at=observed_at)
            self._conn.commit()

    def remove_file(self, scope: str, project: str) -> None:
        """Remove all graph entries for a scope/project."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM relationships WHERE source_scope = ? AND source_project = ?",
                (scope, project),
            )
            self._conn.execute(
                "DELETE FROM entities WHERE scope = ? AND project = ?",
                (scope, project),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def related(
        self,
        entity: str,
        depth: int = 1,
        predicates: list[str] | None = None,
        allowed_scopes: set[str] | None = None,
        as_of: str | None = None,
    ) -> list[dict]:
        """Return entities related to *entity* up to *depth* hops.

        Performs BFS over the graph. Filters by predicates and allowed_scopes
        when provided. When *as_of* is set (ISO 8601 string), only relationships
        valid at that point in time are returned.

        Returns list of {name, entity_type, scope, project, predicate,
                          confidence, distance}.
        """
        norm_entity = _normalize(entity)

        with self._lock:
            # Find starting entity id
            cur = self._conn.execute(
                "SELECT id FROM entities WHERE name = ?",
                (norm_entity,),
            )
            row = cur.fetchone()
            if row is None:
                return []
            start_id = row[0]

            visited: set[int] = {start_id}
            results: list[dict] = []
            queue: deque[tuple[int, int]] = deque([(start_id, 0)])

            pred_filter = ""
            pred_params: list = []
            if predicates:
                placeholders = ",".join("?" * len(predicates))
                pred_filter = f"AND r.predicate IN ({placeholders})"
                pred_params = list(predicates)

            scope_filter = ""
            scope_params: list = []
            if allowed_scopes:
                placeholders = ",".join("?" * len(allowed_scopes))
                scope_filter = f"AND e.scope IN ({placeholders})"
                scope_params = list(allowed_scopes)

            temporal_filter = ""
            temporal_params: list = []
            if as_of is not None:
                temporal_filter = (
                    "AND (r.valid_from IS NULL OR r.valid_from <= ?) "
                    "AND (r.valid_to IS NULL OR r.valid_to > ?)"
                )
                temporal_params = [as_of, as_of]

            sql = f"""
                SELECT e.id, e.name, e.entity_type, e.scope, e.project,
                       r.predicate, r.confidence
                FROM relationships r
                JOIN entities e ON e.id = r.object_id
                WHERE r.subject_id = ?
                {pred_filter}
                {scope_filter}
                {temporal_filter}
                UNION
                SELECT e.id, e.name, e.entity_type, e.scope, e.project,
                       r.predicate, r.confidence
                FROM relationships r
                JOIN entities e ON e.id = r.subject_id
                WHERE r.object_id = ?
                {pred_filter}
                {scope_filter}
                {temporal_filter}
            """

            while queue:
                node_id, dist = queue.popleft()
                if dist >= depth:
                    continue

                params = [
                    node_id, *pred_params, *scope_params, *temporal_params,
                    node_id, *pred_params, *scope_params, *temporal_params,
                ]
                cur = self._conn.execute(sql, params)
                for row in cur.fetchall():
                    nbr_id, name, etype, scope, project, predicate, confidence = row
                    if nbr_id in visited:
                        continue
                    visited.add(nbr_id)
                    results.append(
                        {
                            "name": name,
                            "entity_type": etype,
                            "scope": scope,
                            "project": project,
                            "predicate": predicate,
                            "confidence": confidence,
                            "distance": dist + 1,
                        }
                    )
                    queue.append((nbr_id, dist + 1))

        return results

    def timeline(self, entity: str) -> list[dict]:
        """Return all relationships involving *entity*, sorted by observed_at DESC.

        Shows how the entity's connections evolved over time.

        Returns list of {subject, predicate, object, source_scope, source_project,
                          source_section, confidence, observed_at, valid_from, valid_to}.
        """
        norm_entity = _normalize(entity)

        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM entities WHERE name = ?",
                (norm_entity,),
            )
            row = cur.fetchone()
            if row is None:
                return []
            entity_id = row[0]

            cur = self._conn.execute(
                """
                SELECT
                    es.name AS subject,
                    r.predicate,
                    eo.name AS object,
                    r.source_scope,
                    r.source_project,
                    r.source_section,
                    r.confidence,
                    r.observed_at,
                    r.valid_from,
                    r.valid_to
                FROM relationships r
                JOIN entities es ON es.id = r.subject_id
                JOIN entities eo ON eo.id = r.object_id
                WHERE r.subject_id = ? OR r.object_id = ?
                ORDER BY r.observed_at DESC
                """,
                (entity_id, entity_id),
            )
            return [
                {
                    "subject": row[0],
                    "predicate": row[1],
                    "object": row[2],
                    "source_scope": row[3],
                    "source_project": row[4],
                    "source_section": row[5],
                    "confidence": row[6],
                    "observed_at": row[7],
                    "valid_from": row[8],
                    "valid_to": row[9],
                }
                for row in cur.fetchall()
            ]

    def entity_info(self, entity: str) -> dict | None:
        """Return info about a single entity including its relationship count."""
        norm_entity = _normalize(entity)
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT e.id, e.name, e.entity_type, e.scope, e.project,
                       (SELECT COUNT(*) FROM relationships r
                        WHERE r.subject_id = e.id OR r.object_id = e.id
                       ) AS relationship_count
                FROM entities e
                WHERE e.name = ?
                LIMIT 1
                """,
                (norm_entity,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "name": row[1],
                "entity_type": row[2],
                "scope": row[3],
                "project": row[4],
                "relationship_count": row[5],
            }

    def list_entities(self, scope: str | None = None) -> list[dict]:
        """List all entities, optionally filtered by scope.

        Returns list of {name, entity_type, scope, project, relationship_count}.
        """
        with self._lock:
            if scope is not None:
                cur = self._conn.execute(
                    """
                    SELECT e.name, e.entity_type, e.scope, e.project,
                           (SELECT COUNT(*) FROM relationships r
                            WHERE r.subject_id = e.id OR r.object_id = e.id
                           ) AS relationship_count
                    FROM entities e
                    WHERE e.scope = ?
                    ORDER BY e.scope, e.project, e.name
                    """,
                    (scope,),
                )
            else:
                cur = self._conn.execute(
                    """
                    SELECT e.name, e.entity_type, e.scope, e.project,
                           (SELECT COUNT(*) FROM relationships r
                            WHERE r.subject_id = e.id OR r.object_id = e.id
                           ) AS relationship_count
                    FROM entities e
                    ORDER BY e.scope, e.project, e.name
                    """
                )
            return [
                {
                    "name": row[0],
                    "entity_type": row[1],
                    "scope": row[2],
                    "project": row[3],
                    "relationship_count": row[4],
                }
                for row in cur.fetchall()
            ]
