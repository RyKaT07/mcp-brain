"""Top-level orchestrator for the embedding subsystem.

Owns one ``Embedder`` and one or more ``VectorStore`` instances (one
global, plus a lazily-created store per user under ``users/<uid>``).

Public surface:
- ``EmbeddingService.refresh_file(scope, project, content)`` — diff-aware
  re-embed of a single file; called from the ``knowledge_update``
  write hook.
- ``EmbeddingService.delete_file(scope, project)`` — drop every chunk
  for a deleted file; called from the ``knowledge_delete`` write hook.
- ``EmbeddingService.bootstrap(knowledge_dir)`` — walk the directory
  on startup and embed any file whose chunks are missing.
- ``EmbeddingService.search(query, k, scope, allowed_scopes)`` —
  semantic-search query.

The whole subsystem is optional: if ``fastembed`` or ``sqlite-vec``
isn't installed, ``EmbeddingService.create_or_none()`` returns ``None``
and the rest of the server keeps working without semantic search.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from mcp_brain.embeddings.chunker import Chunk, chunk_markdown
from mcp_brain.embeddings.embedder import Embedder
from mcp_brain.embeddings.store import VectorStore, _resolve_default_path

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Coordinates chunker + embedder + global / per-user vector stores."""

    def __init__(
        self,
        *,
        knowledge_dir: Path,
        embedder: Embedder | None = None,
        global_store_path: Path | None = None,
    ) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        self.embedder = embedder or Embedder()
        self._global_store = VectorStore(
            global_store_path or _resolve_default_path(),
            dim=self.embedder.dim,
        )
        self._user_stores: dict[str, VectorStore] = {}
        self._user_lock = threading.Lock()

    # ── Factory ──────────────────────────────────────────────────

    @classmethod
    def create_or_none(cls, knowledge_dir: Path) -> "EmbeddingService | None":
        """Build a service if optional deps are installed, else return None.

        The ``MCP_DISABLE_EMBEDDINGS`` env var is an explicit kill-switch
        for environments where we want to skip the model download (e.g.
        air-gapped CI).
        """
        if os.getenv("MCP_DISABLE_EMBEDDINGS", "").strip().lower() in ("1", "true", "yes"):
            logger.info("embeddings disabled by MCP_DISABLE_EMBEDDINGS")
            return None
        try:
            embedder = Embedder()
            return cls(knowledge_dir=knowledge_dir, embedder=embedder)
        except RuntimeError as e:
            logger.warning("embeddings disabled: %s", e)
            return None

    # ── Per-user store routing ───────────────────────────────────

    def store_for(self, user_id: str | None) -> VectorStore:
        """Return the user's store, creating it on first use.

        ``user_id=None`` returns the global store. Each per-user store
        lives at ``knowledge_dir/users/<uid>/_index/embeddings.db``,
        mirroring the per-user search index and graph layout.
        """
        if user_id is None:
            return self._global_store
        with self._user_lock:
            store = self._user_stores.get(user_id)
            if store is not None:
                return store
            path = (
                self.knowledge_dir / "users" / user_id / "_index" / "embeddings.db"
            )
            store = VectorStore(path, dim=self.embedder.dim)
            self._user_stores[user_id] = store
            return store

    # ── Refresh / delete hooks ───────────────────────────────────

    def refresh_file(
        self,
        scope: str,
        project: str,
        content: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, int]:
        """Diff-aware re-embed of one file.

        Returns ``{embedded, reused, removed}`` counts so callers (or
        the maintain loop) can log activity.
        """
        store = self.store_for(user_id)
        new_chunks = chunk_markdown(scope, project, content)
        existing = store.get_hashes(scope, project)

        new_ids = {c.chunk_id for c in new_chunks}
        # Drop chunks that no longer exist (heading deleted, file shorter).
        removed = [cid for cid in existing if cid not in new_ids]
        if removed:
            store.delete_chunks(removed)

        # Identify chunks whose content actually changed.
        to_embed: list[Chunk] = [
            c for c in new_chunks if existing.get(c.chunk_id) != c.content_hash
        ]
        reused = len(new_chunks) - len(to_embed)

        if to_embed:
            vectors = self.embedder.encode([c.text for c in to_embed])
            for chunk, vec in zip(to_embed, vectors, strict=True):
                store.upsert(
                    chunk.chunk_id,
                    scope=chunk.scope,
                    project=chunk.project,
                    heading_path=chunk.heading_path,
                    chunk_idx=chunk.chunk_idx,
                    content_hash=chunk.content_hash,
                    text=chunk.text,
                    embedding=vec,
                )

        return {
            "embedded": len(to_embed),
            "reused": reused,
            "removed": len(removed),
        }

    def delete_file(
        self,
        scope: str,
        project: str,
        *,
        user_id: str | None = None,
    ) -> int:
        store = self.store_for(user_id)
        return store.delete_file(scope, project)

    # ── Bootstrap on startup ─────────────────────────────────────

    def bootstrap(
        self,
        knowledge_dir: Path | None = None,
        *,
        user_id: str | None = None,
    ) -> dict[str, int]:
        """Walk a knowledge directory and (re-)embed any file that has
        zero chunks in the store. Idempotent: existing chunks are kept.

        For incremental rebuilds the diff-aware refresh in
        ``refresh_file`` handles changed content. ``bootstrap`` is the
        one-time fill on a fresh DB.
        """
        base = Path(knowledge_dir) if knowledge_dir is not None else self.knowledge_dir
        store = self.store_for(user_id)
        embedded = 0
        skipped = 0
        if not base.exists():
            return {"embedded": 0, "skipped": 0, "files": 0}
        files = 0
        for md in base.glob("*/*.md"):
            scope = md.parent.name
            project = md.stem
            if scope.startswith(("_", ".")):
                continue
            # Multi-user knowledge layout has files under
            # ``knowledge_dir/users/<uid>/<scope>/<file>.md``. Skip the
            # ``users`` directory at this level — ``bootstrap_all``
            # iterates per-user and embeds those into the right
            # per-user stores. Without this guard ``users/<uid>.md``
            # would never match (it's a directory, not a file), but
            # any stray ``users/random.md`` would land in the global
            # store under the wrong scope name.
            if scope == "users":
                continue
            files += 1
            existing = store.get_hashes(scope, project)
            if existing:
                skipped += 1
                continue
            try:
                content = md.read_text(encoding="utf-8")
            except OSError:
                continue
            stats = self.refresh_file(scope, project, content, user_id=user_id)
            embedded += stats["embedded"]
        return {"embedded": embedded, "skipped": skipped, "files": files}

    def bootstrap_all(self) -> dict[str, dict[str, int]]:
        """Bootstrap the root vault and every per-user vault.

        Walks ``self.knowledge_dir`` (root → global store) and every
        ``self.knowledge_dir/users/<uid>/`` directory (→ per-user
        store keyed by ``<uid>``). Returns a mapping of target name
        (``"root"`` or the user id) to the standard
        ``{embedded, skipped, files}`` stats.

        Use this on startup of the unified server (``server.py``)
        which serves multiple users from one process. The bwrap
        worker (``worker.py``) gets a user-scoped knowledge dir
        bind-mounted by the manager, so it just calls ``bootstrap``
        directly.
        """
        out: dict[str, dict[str, int]] = {}
        out["root"] = self.bootstrap(self.knowledge_dir)
        users_dir = self.knowledge_dir / "users"
        if users_dir.is_dir():
            for child in sorted(users_dir.iterdir()):
                if not child.is_dir():
                    continue
                if child.name.startswith((".", "_")):
                    continue
                out[child.name] = self.bootstrap(child, user_id=child.name)
        return out

    # ── Query ────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        k: int = 10,
        scope: str | None = None,
        allowed_scopes: set[str] | None = None,
        user_id: str | None = None,
    ) -> list[dict]:
        """Semantic-search the knowledge store.

        Combines hits from the global store and (when ``user_id`` is
        set) the per-user store, sorted by ascending distance, deduped
        by ``(scope, project, heading_path)``.
        """
        if not query.strip():
            return []
        query_vec = self.embedder.encode_one(query)
        hits = self._global_store.knn(
            query_vec, k=k, scope=scope, allowed_scopes=allowed_scopes
        )
        if user_id is not None:
            user_store = self.store_for(user_id)
            if user_store is not self._global_store:
                hits.extend(
                    user_store.knn(
                        query_vec, k=k, scope=scope, allowed_scopes=allowed_scopes
                    )
                )
        # Dedupe by (scope, project, heading_path) keeping the smaller distance.
        deduped: dict[tuple[str, str, str], dict] = {}
        for h in hits:
            key = (h["scope"], h["project"], h["heading_path"])
            cur = deduped.get(key)
            if cur is None or h["distance"] < cur["distance"]:
                deduped[key] = h
        out = sorted(deduped.values(), key=lambda h: h["distance"])
        return out[:k]
