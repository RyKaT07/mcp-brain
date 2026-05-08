"""Tests for the in-memory ``knowledge_graph`` payload cache.

These tests target the cache module directly and verify the wire-up
into ``compute_graph`` via the tool wrapper. Hooks (``invalidate``
called from ``knowledge_update`` / ``knowledge_delete``) are covered
in ``test_knowledge_graph_tool.py`` and the existing knowledge-write
tests so the per-write counters stay accurate without monkeypatching
half the suite here.
"""

from __future__ import annotations

import time

import pytest

from mcp_brain import graph_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    graph_cache._reset_for_test()
    yield
    graph_cache._reset_for_test()


class TestCacheKey:
    def test_keys_with_same_inputs_are_equal(self):
        a = graph_cache.make_key(
            user_id="alice",
            scope="work",
            include_semantic=True,
            include_tags=True,
            allowed_scopes={"work", "school"},
        )
        b = graph_cache.make_key(
            user_id="alice",
            scope="work",
            include_semantic=True,
            include_tags=True,
            # Same set, different insertion order — sorted tuple
            # canonicalises this so the keys match.
            allowed_scopes={"school", "work"},
        )
        assert a == b
        assert hash(a) == hash(b)

    def test_keys_differ_on_user_or_flags(self):
        base = dict(
            scope=None,
            include_semantic=True,
            include_tags=True,
            allowed_scopes=None,
        )
        k_alice = graph_cache.make_key(user_id="alice", **base)
        k_bob = graph_cache.make_key(user_id="bob", **base)
        k_no_sem = graph_cache.make_key(
            user_id="alice",
            scope=None,
            include_semantic=False,
            include_tags=True,
            allowed_scopes=None,
        )
        assert k_alice != k_bob
        assert k_alice != k_no_sem


class TestGetSet:
    def test_miss_returns_none(self):
        key = graph_cache.make_key(
            user_id="alice",
            scope=None,
            include_semantic=True,
            include_tags=True,
            allowed_scopes=None,
        )
        assert graph_cache.get(key) is None

    def test_hit_returns_payload(self):
        key = graph_cache.make_key(
            user_id="alice",
            scope=None,
            include_semantic=True,
            include_tags=True,
            allowed_scopes=None,
        )
        graph_cache.set(key, '{"nodes":[]}')
        assert graph_cache.get(key) == '{"nodes":[]}'

    def test_overwrite_replaces_payload(self):
        key = graph_cache.make_key(
            user_id="alice",
            scope=None,
            include_semantic=True,
            include_tags=True,
            allowed_scopes=None,
        )
        graph_cache.set(key, "v1")
        graph_cache.set(key, "v2")
        assert graph_cache.get(key) == "v2"

    def test_get_with_non_cachekey_is_safe(self):
        # Defensive: a caller passing a tuple/string instead of a
        # CacheKey should not crash.
        assert graph_cache.get(("not", "a", "key")) is None
        assert graph_cache.get("plain string") is None


class TestTTL:
    def test_expired_entry_returns_none_and_is_evicted(self):
        # Tight TTL so the test stays fast.
        graph_cache._set_ttl_for_test(0.05)
        key = graph_cache.make_key(
            user_id="alice",
            scope=None,
            include_semantic=True,
            include_tags=True,
            allowed_scopes=None,
        )
        graph_cache.set(key, "old")
        time.sleep(0.06)
        assert graph_cache.get(key) is None
        # Lazy eviction: the get() call dropped the stale entry.
        assert graph_cache._size_for_test() == 0


class TestInvalidate:
    def test_invalidate_drops_only_that_users_entries(self):
        common = dict(
            scope=None,
            include_semantic=True,
            include_tags=True,
            allowed_scopes=None,
        )
        alice = graph_cache.make_key(user_id="alice", **common)
        bob = graph_cache.make_key(user_id="bob", **common)
        root = graph_cache.make_key(user_id=None, **common)
        graph_cache.set(alice, "A")
        graph_cache.set(bob, "B")
        graph_cache.set(root, "R")

        removed = graph_cache.invalidate("alice")
        assert removed == 1
        assert graph_cache.get(alice) is None
        # Other users untouched.
        assert graph_cache.get(bob) == "B"
        assert graph_cache.get(root) == "R"

    def test_invalidate_root_user(self):
        common = dict(
            scope=None,
            include_semantic=True,
            include_tags=True,
            allowed_scopes=None,
        )
        alice = graph_cache.make_key(user_id="alice", **common)
        root = graph_cache.make_key(user_id=None, **common)
        graph_cache.set(alice, "A")
        graph_cache.set(root, "R")

        removed = graph_cache.invalidate(None)
        assert removed == 1
        assert graph_cache.get(root) is None
        assert graph_cache.get(alice) == "A"

    def test_invalidate_drops_every_per_user_filter_combination(self):
        # Same user, multiple filter combinations — invalidate must
        # take all of them out (can't be surgical because semantic
        # edges fan out across scopes).
        common_user = dict(user_id="alice")
        graph_cache.set(
            graph_cache.make_key(
                **common_user,
                scope=None,
                include_semantic=True,
                include_tags=True,
                allowed_scopes=None,
            ),
            "1",
        )
        graph_cache.set(
            graph_cache.make_key(
                **common_user,
                scope="work",
                include_semantic=True,
                include_tags=True,
                allowed_scopes={"work"},
            ),
            "2",
        )
        graph_cache.set(
            graph_cache.make_key(
                **common_user,
                scope=None,
                include_semantic=False,
                include_tags=True,
                allowed_scopes=None,
            ),
            "3",
        )

        removed = graph_cache.invalidate("alice")
        assert removed == 3
        assert graph_cache._size_for_test() == 0
