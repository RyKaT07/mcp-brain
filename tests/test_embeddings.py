"""Tests for the embedding subsystem.

The model itself (fastembed) is replaced with a deterministic stand-in
in every test that touches the service / store layer, so the tests
don't need network access or the actual model weights. The chunker
tests are pure-Python and have no deps.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mcp_brain.embeddings.chunker import chunk_markdown
from mcp_brain.embeddings.embedder import KNOWN_DIMS

# ── Detect optional deps ────────────────────────────────────────────

try:
    import sqlite_vec  # noqa: F401
    import fastembed  # noqa: F401

    EMBED_DEPS_AVAILABLE = True
except ImportError:
    EMBED_DEPS_AVAILABLE = False


needs_embed_deps = pytest.mark.skipif(
    not EMBED_DEPS_AVAILABLE,
    reason="optional deps fastembed/sqlite-vec not installed",
)


# ── Chunker (no optional deps) ──────────────────────────────────────


class TestChunker:
    def test_empty_input_yields_no_chunks(self):
        assert chunk_markdown("work", "x", "") == []
        assert chunk_markdown("work", "x", "   \n\n  ") == []

    def test_single_section(self):
        md = "## Overview\nShort body."
        chunks = chunk_markdown("work", "doc", md)
        assert len(chunks) == 1
        c = chunks[0]
        assert c.scope == "work"
        assert c.project == "doc"
        assert c.heading_path == "Overview"
        assert "## Overview" in c.text
        assert "Short body." in c.text
        assert c.chunk_idx == 0
        assert len(c.chunk_id) == 32

    def test_preamble_becomes_chunk(self):
        md = "Some intro text without a heading.\n## After"
        chunks = chunk_markdown("work", "doc", md)
        assert len(chunks) == 2
        assert chunks[0].heading_path == "_preamble"
        assert "intro text" in chunks[0].text
        assert chunks[1].heading_path == "After"

    def test_multiple_sections(self):
        md = "## A\nbody a\n## B\nbody b\n## C\nbody c"
        chunks = chunk_markdown("work", "doc", md)
        paths = [c.heading_path for c in chunks]
        assert paths == ["A", "B", "C"]

    def test_chunk_ids_are_stable_across_runs(self):
        md = "## A\nbody"
        a = chunk_markdown("work", "doc", md)
        b = chunk_markdown("work", "doc", md)
        assert a[0].chunk_id == b[0].chunk_id
        assert a[0].content_hash == b[0].content_hash

    def test_content_hash_changes_when_body_edits(self):
        a = chunk_markdown("work", "doc", "## A\noriginal")
        b = chunk_markdown("work", "doc", "## A\nedited")
        assert a[0].chunk_id == b[0].chunk_id  # same heading + idx → same id
        assert a[0].content_hash != b[0].content_hash

    def test_long_section_splits_on_h3(self):
        long_para = " ".join(["word"] * 350)
        md = (
            "## Big section\nintro\n"
            f"### First\n{long_para}\n"
            f"### Second\n{long_para}"
        )
        chunks = chunk_markdown("work", "doc", md)
        # Expect H3 sub-splitting: at least one chunk per H3.
        h3_paths = [c.heading_path for c in chunks if "/" in c.heading_path]
        assert any("First" in p for p in h3_paths)
        assert any("Second" in p for p in h3_paths)

    def test_long_section_without_h3_falls_back_to_window(self):
        md = "## Single\n" + " ".join(["word"] * 800)
        chunks = chunk_markdown("work", "doc", md)
        assert len(chunks) >= 2
        assert all(c.heading_path == "Single" or c.heading_path == "_preamble" for c in chunks)


# ── Embedder dim catalogue ──────────────────────────────────────────


class TestEmbedderDims:
    def test_default_dim_known(self):
        assert KNOWN_DIMS["BAAI/bge-small-en-v1.5"] == 384

    def test_bge_m3_dim(self):
        assert KNOWN_DIMS["BAAI/bge-m3"] == 1024


# ── Service with a fake embedder ────────────────────────────────────


@needs_embed_deps
class TestServiceWithFakeEmbedder:
    """Exercise refresh_file / search end-to-end with a deterministic stub."""

    @pytest.fixture
    def fake_embedder(self):
        """Hash-based 'embedder': maps text → 8d vector deterministically.

        Same text → same vector → cosine-perfect match. Different text →
        different vector. Plenty for testing the upsert / KNN plumbing
        without loading a real model.
        """
        from mcp_brain.embeddings.embedder import Embedder

        class FakeEmbedder(Embedder):
            def __init__(self):
                self.model_name = "fake/test"
                self.dim = 8

            def encode(self, texts):
                out = []
                for t in texts:
                    h = hashlib.sha256(t.encode("utf-8")).digest()
                    # 8 floats in [0, 1) — enough to discriminate.
                    out.append([h[i] / 255.0 for i in range(self.dim)])
                return out

            def encode_one(self, text):
                return self.encode([text])[0]

        return FakeEmbedder()

    @pytest.fixture
    def service(self, tmp_path: Path, fake_embedder):
        from mcp_brain.embeddings.service import EmbeddingService

        return EmbeddingService(
            knowledge_dir=tmp_path,
            embedder=fake_embedder,
            global_store_path=tmp_path / "embeddings.db",
        )

    def test_refresh_embeds_fresh_file(self, service):
        stats = service.refresh_file("work", "x", "## A\nfirst\n## B\nsecond")
        assert stats["embedded"] == 2
        assert stats["reused"] == 0
        assert stats["removed"] == 0

    def test_refresh_reuses_unchanged_chunks(self, service):
        service.refresh_file("work", "x", "## A\none\n## B\ntwo")
        stats = service.refresh_file("work", "x", "## A\none\n## B\ntwo")
        assert stats["embedded"] == 0
        assert stats["reused"] == 2
        assert stats["removed"] == 0

    def test_refresh_only_embeds_changed_chunk(self, service):
        service.refresh_file("work", "x", "## A\none\n## B\ntwo")
        # Edit only section B.
        stats = service.refresh_file("work", "x", "## A\none\n## B\nchanged")
        assert stats["embedded"] == 1
        assert stats["reused"] == 1
        assert stats["removed"] == 0

    def test_refresh_drops_removed_section(self, service):
        service.refresh_file("work", "x", "## A\none\n## B\ntwo")
        stats = service.refresh_file("work", "x", "## A\none")
        assert stats["embedded"] == 0
        assert stats["reused"] == 1
        assert stats["removed"] == 1

    def test_search_returns_exact_match_first(self, service):
        service.refresh_file("work", "doc", "## A\napple banana\n## B\ndog cat")
        # The fake embedder is content-hash-keyed, so an identical text
        # query produces a zero-distance hit on that exact chunk.
        a_text = "## A\napple banana"
        hits = service.search(a_text, k=2)
        assert hits, "expected at least one hit"
        assert hits[0]["scope"] == "work"
        assert hits[0]["project"] == "doc"
        assert hits[0]["heading_path"] == "A"

    def test_delete_file_removes_all_chunks(self, service):
        service.refresh_file("work", "x", "## A\none\n## B\ntwo")
        n = service.delete_file("work", "x")
        assert n == 2
        assert service._global_store.chunk_count() == 0

    def test_audit_coherence_empty_store(self, service):
        out = service.audit_coherence()
        assert out["chunk_count"] == 0
        assert out["low_coherence"] == []
        assert out["duplicate_suspects"] == []

    def test_audit_coherence_flags_orphan_chunks(self, service):
        # Three files. The fake embedder is sha256-based so each
        # distinct text maps to a deterministic point in [0,1)^8 with
        # no clustering. With the default low_coherence_distance of
        # 1.0 (cos≈0.5 on normalised vectors) and these random-ish
        # vectors, none should fall under the threshold — every chunk
        # has plenty of neighbours.
        service.refresh_file("work", "a", "## A\nalpha content unique")
        service.refresh_file("work", "b", "## B\nbeta content unique")
        service.refresh_file("work", "c", "## C\ngamma content unique")
        out = service.audit_coherence(low_coherence_distance=0.0)
        # Threshold 0.0 forces every chunk to qualify as low-coherence
        # since dist > 0 between distinct sha256 buckets.
        assert out["chunk_count"] == 3
        assert len(out["low_coherence"]) == 3

    def test_audit_coherence_flags_cross_file_duplicates(self, service):
        # Same text in two different files → identical fake embedding
        # → distance 0. With duplicate_distance high enough they get
        # flagged as a duplicate-suspect pair.
        service.refresh_file("work", "a", "## Topic\nshared body")
        service.refresh_file("homelab", "a", "## Topic\nshared body")
        # Plus a non-duplicate so we know we filter intelligently.
        service.refresh_file("school", "a", "## Topic\nunrelated body")
        out = service.audit_coherence(duplicate_distance=0.01)
        # Exactly one cross-file duplicate pair.
        assert len(out["duplicate_suspects"]) == 1
        pair = out["duplicate_suspects"][0]
        scopes = sorted([pair["a"]["scope"], pair["b"]["scope"]])
        assert scopes == ["homelab", "work"]

    def test_audit_coherence_skips_same_file_pairs(self, service):
        # Two chunks with identical text in the same file are not a
        # cross-file duplicate, so the rule shouldn't fire.
        service.refresh_file(
            "work", "x", "## A\nshared body\n## B\nshared body"
        )
        out = service.audit_coherence(duplicate_distance=0.01)
        assert out["duplicate_suspects"] == []

    def test_per_user_store_is_isolated(self, service, tmp_path: Path):
        # Same scope+project, different user_id → separate store.
        (tmp_path / "users" / "alice").mkdir(parents=True)
        (tmp_path / "users" / "bob").mkdir(parents=True)
        service.refresh_file("work", "x", "## A\nalice", user_id="alice")
        service.refresh_file("work", "x", "## A\nbob", user_id="bob")
        # Each user's store has 1 chunk; they don't see each other.
        alice = service.store_for("alice").chunk_count()
        bob = service.store_for("bob").chunk_count()
        assert alice == 1
        assert bob == 1

    def test_bootstrap_walks_knowledge_dir(self, tmp_path: Path, fake_embedder):
        from mcp_brain.embeddings.service import EmbeddingService

        scope_dir = tmp_path / "homelab"
        scope_dir.mkdir()
        (scope_dir / "alpha.md").write_text("## A\ncontent a")
        (scope_dir / "beta.md").write_text("## B\ncontent b")
        # Hidden / meta scopes should be skipped.
        meta = tmp_path / "_meta"
        meta.mkdir()
        (meta / "skip.md").write_text("## X\nignored")

        svc = EmbeddingService(
            knowledge_dir=tmp_path,
            embedder=fake_embedder,
            global_store_path=tmp_path / "embeddings.db",
        )
        stats = svc.bootstrap()
        assert stats["files"] == 2
        assert stats["embedded"] == 2
        assert stats["skipped"] == 0
        # Second bootstrap is a no-op.
        again = svc.bootstrap()
        assert again["embedded"] == 0
        assert again["skipped"] == 2

    def test_bootstrap_skips_users_dir_at_root(
        self, tmp_path: Path, fake_embedder
    ):
        """Files under ``users/<uid>/`` must not be embedded into the
        global store as if their parent were a regular scope. They
        belong in per-user stores and ``bootstrap_all`` handles them.
        """
        from mcp_brain.embeddings.service import EmbeddingService

        # One legit root-scope file.
        root_scope = tmp_path / "homelab"
        root_scope.mkdir()
        (root_scope / "alpha.md").write_text("## A\nroot content")
        # A multi-user vault where files live under users/<uid>/<scope>/.
        # The OLD bootstrap globbed ``*/*.md`` and never matched these
        # at depth 3. The new bootstrap explicitly skips ``users`` at
        # the root level so ``bootstrap_all`` is the only path that
        # embeds them.
        users_alpha = tmp_path / "users" / "alice" / "work"
        users_alpha.mkdir(parents=True)
        (users_alpha / "deep.md").write_text("## A\nalice content")

        svc = EmbeddingService(
            knowledge_dir=tmp_path,
            embedder=fake_embedder,
            global_store_path=tmp_path / "embeddings.db",
        )
        stats = svc.bootstrap()
        # Only the legit root-scope file is counted; ``users`` skipped.
        assert stats["files"] == 1
        assert stats["embedded"] == 1

    def test_bootstrap_all_walks_root_and_each_user(
        self, tmp_path: Path, fake_embedder
    ):
        """Multi-user setup: root vault + ``users/<uid>/`` vaults each
        get their own bootstrap pass into the right store.
        """
        from mcp_brain.embeddings.service import EmbeddingService

        # Root vault — Patryk's single-user knowledge.
        (tmp_path / "homelab").mkdir()
        (tmp_path / "homelab" / "alpha.md").write_text("## A\nroot")
        # Two per-user vaults.
        (tmp_path / "users" / "alice" / "work").mkdir(parents=True)
        (tmp_path / "users" / "alice" / "work" / "a.md").write_text(
            "## A\nalice"
        )
        (tmp_path / "users" / "bob" / "school").mkdir(parents=True)
        (tmp_path / "users" / "bob" / "school" / "b.md").write_text(
            "## B\nbob1"
        )
        (tmp_path / "users" / "bob" / "school" / "c.md").write_text(
            "## C\nbob2"
        )
        # Hidden user dir is skipped (matches the `_`/`.` rule for scopes).
        (tmp_path / "users" / "_internal" / "x").mkdir(parents=True)
        (tmp_path / "users" / "_internal" / "x" / "x.md").write_text(
            "## X\nignored"
        )

        svc = EmbeddingService(
            knowledge_dir=tmp_path,
            embedder=fake_embedder,
            global_store_path=tmp_path / "embeddings.db",
        )
        stats = svc.bootstrap_all()
        # Three keys: root + two real users.
        assert set(stats) == {"root", "alice", "bob"}
        assert stats["root"]["files"] == 1
        assert stats["alice"]["files"] == 1
        assert stats["bob"]["files"] == 2
        # Per-user stores are isolated from the global one.
        assert svc._global_store.chunk_count() == 1
        assert svc.store_for("alice").chunk_count() == 1
        assert svc.store_for("bob").chunk_count() == 2

    def test_bootstrap_re_embeds_stale_file_modified_offline(
        self, tmp_path: Path, fake_embedder
    ):
        """If a markdown file is edited while the brain is offline (manual
        edit, git pull, Syncthing), the next bootstrap MUST detect the
        per-chunk hash drift and re-embed the changed sections — not
        skip the file because it already has chunks.
        """
        from mcp_brain.embeddings.service import EmbeddingService

        scope = tmp_path / "homelab"
        scope.mkdir()
        target = scope / "alpha.md"
        target.write_text("## A\noriginal content")

        svc = EmbeddingService(
            knowledge_dir=tmp_path,
            embedder=fake_embedder,
            global_store_path=tmp_path / "embeddings.db",
        )
        first = svc.bootstrap()
        assert first["files"] == 1
        assert first["embedded"] == 1
        assert first["refreshed"] == 1
        assert first["skipped"] == 0

        # Mutate the file out-of-band, then bootstrap again.
        target.write_text("## A\ncompletely different body")
        second = svc.bootstrap()

        assert second["files"] == 1
        # The content_hash changed → the chunk must be re-embedded.
        assert second["embedded"] == 1
        assert second["refreshed"] == 1
        assert second["skipped"] == 0

        # And a third pass with no changes is a true no-op again.
        third = svc.bootstrap()
        assert third["embedded"] == 0
        assert third["skipped"] == 1
        assert third["refreshed"] == 0

    def test_bootstrap_drops_chunks_for_removed_section(
        self, tmp_path: Path, fake_embedder
    ):
        """A section deleted from the file while the brain was offline
        must have its chunks removed by bootstrap, not orphaned."""
        from mcp_brain.embeddings.service import EmbeddingService

        scope = tmp_path / "homelab"
        scope.mkdir()
        target = scope / "alpha.md"
        target.write_text("## A\nfirst\n\n## B\nsecond")

        svc = EmbeddingService(
            knowledge_dir=tmp_path,
            embedder=fake_embedder,
            global_store_path=tmp_path / "embeddings.db",
        )
        svc.bootstrap()
        store = svc._global_store
        assert store.chunk_count() == 2

        # Delete section B out-of-band.
        target.write_text("## A\nfirst")
        stats = svc.bootstrap()
        assert stats["refreshed"] == 1
        assert stats["embedded"] == 0  # A unchanged
        assert store.chunk_count() == 1
