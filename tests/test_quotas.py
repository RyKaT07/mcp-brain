"""Tests for the per-plan worker quota mapping + auth-driven resolver."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mcp_brain.auth import YamlTokenVerifier
from mcp_brain.isolation.quotas import (
    DEFAULT_PLAN,
    PLAN_QUOTAS,
    quotas_for_plan,
)


class TestQuotasForPlan:
    def test_known_plans_return_dedicated_quotas(self):
        for name in ("free", "trial", "personal", "pro", "tester"):
            q = quotas_for_plan(name)
            assert q == PLAN_QUOTAS[name]

    def test_unknown_plan_falls_back_to_free(self):
        assert quotas_for_plan("enterprise") == PLAN_QUOTAS[DEFAULT_PLAN]

    def test_none_falls_back_to_free(self):
        # Tokens without a plan field shouldn't accidentally land on
        # pro-tier quotas.
        assert quotas_for_plan(None) == PLAN_QUOTAS[DEFAULT_PLAN]

    def test_pro_strictly_outranks_personal(self):
        # Sanity: paying-tier upgrade is real, not a typo.
        pro = PLAN_QUOTAS["pro"]
        personal = PLAN_QUOTAS["personal"]
        assert pro["memory_max"] > personal["memory_max"]
        assert pro["cpu_pct"] > personal["cpu_pct"]
        assert pro["pids_max"] > personal["pids_max"]

    def test_free_is_strict_lower_bound(self):
        free = PLAN_QUOTAS["free"]
        for tier in ("trial", "personal", "pro", "tester"):
            t = PLAN_QUOTAS[tier]
            assert t["memory_max"] >= free["memory_max"]
            assert t["cpu_pct"] >= free["cpu_pct"]
            assert t["pids_max"] >= free["pids_max"]


class TestPlanForUserId:
    """``YamlTokenVerifier.plan_for_user_id`` reads ``plan`` off any
    token entry that has a matching ``user_id``."""

    def _write_yaml(self, path: Path, tokens: list[dict]):
        path.write_text(
            yaml.safe_dump({"tokens": tokens}, sort_keys=False),
            encoding="utf-8",
        )

    def test_returns_plan_when_token_carries_one(self, tmp_path: Path):
        cfg = tmp_path / "auth.yaml"
        self._write_yaml(
            cfg,
            [
                {
                    "id": "alice-laptop",
                    "token": "tok_alice_xxxxxxxx",
                    "scopes": ["*"],
                    "user_id": "alice",
                    "plan": "pro",
                },
                {
                    "id": "bob-laptop",
                    "token": "tok_bob_xxxxxxxx",
                    "scopes": ["*"],
                    "user_id": "bob",
                    "plan": "personal",
                },
            ],
        )
        v = YamlTokenVerifier(cfg, reload_interval=3600, enable_sighup=False)
        assert v.plan_for_user_id("alice") == "pro"
        assert v.plan_for_user_id("bob") == "personal"

    def test_returns_none_when_token_has_no_plan_field(
        self, tmp_path: Path
    ):
        cfg = tmp_path / "auth.yaml"
        self._write_yaml(
            cfg,
            [
                {
                    "id": "carol",
                    "token": "tok_carol_xxxxxxxx",
                    "scopes": ["*"],
                    "user_id": "carol",
                    # no `plan` key — caller should fall back to free
                },
            ],
        )
        v = YamlTokenVerifier(cfg, reload_interval=3600, enable_sighup=False)
        assert v.plan_for_user_id("carol") is None

    def test_returns_none_when_user_id_unknown(self, tmp_path: Path):
        cfg = tmp_path / "auth.yaml"
        self._write_yaml(
            cfg,
            [
                {
                    "id": "alice",
                    "token": "tok_alice_xxxxxxxx",
                    "scopes": ["*"],
                    "user_id": "alice",
                    "plan": "pro",
                },
            ],
        )
        v = YamlTokenVerifier(cfg, reload_interval=3600, enable_sighup=False)
        assert v.plan_for_user_id("nobody") is None

    def test_pipes_into_quotas_for_plan(self, tmp_path: Path):
        # End-to-end shape check: the resolver's output is the input
        # ProcessManager will hand to quotas_for_plan().
        cfg = tmp_path / "auth.yaml"
        self._write_yaml(
            cfg,
            [
                {
                    "id": "dave",
                    "token": "tok_dave_xxxxxxxx",
                    "scopes": ["*"],
                    "user_id": "dave",
                    "plan": "free",
                },
            ],
        )
        v = YamlTokenVerifier(cfg, reload_interval=3600, enable_sighup=False)
        plan = v.plan_for_user_id("dave")
        assert quotas_for_plan(plan) == PLAN_QUOTAS["free"]


@pytest.mark.parametrize(
    "plan, key",
    [
        ("free", "free"),
        ("trial", "trial"),
        ("personal", "personal"),
        ("pro", "pro"),
        ("tester", "tester"),
        (None, "free"),
        ("nonsense", "free"),
    ],
)
def test_quotas_for_plan_table(plan, key):
    assert quotas_for_plan(plan) == PLAN_QUOTAS[key]
