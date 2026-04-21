"""Tests for the /admin/* HTTP endpoints on the isolation entrypoint.

These endpoints are the only Panel → brain control surface that must be
authenticated.  They're gated by ``BRAIN_ADMIN_TOKEN``; when unset the
endpoints respond 404 (fail-closed, no existence disclosure).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def app_factory(tmp_path: Path, monkeypatch):
    """Build a Starlette app wired to real ProcessManager + stubbed auth.

    Yields a factory that takes an ``admin_token`` and returns the app.
    Environment manipulation happens inside the factory so different tests
    can set different tokens (including "unset").
    """
    from mcp_brain.isolation import entrypoint as ep
    from mcp_brain.isolation.manager import ProcessManager, WorkerInfo
    from starlette.testclient import TestClient

    def _make(admin_token: str | None, *, workers: dict | None = None):
        # Reset module-level ADMIN_TOKEN (it's read once at import time but
        # the handler reads it from the module attribute on every call, so
        # patching here is sufficient).
        monkeypatch.setattr(ep, "ADMIN_TOKEN", admin_token or "")

        pm = ProcessManager(
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )
        for user_id, proc_spec in (workers or {}).items():
            mock_proc = MagicMock(spec=subprocess.Popen)
            mock_proc.poll.return_value = proc_spec.get("poll", None)
            pm._workers[user_id] = WorkerInfo(
                user_id=user_id,
                pid=proc_spec.get("pid", 999),
                socket_path=tmp_path / "sockets" / f"{user_id}.sock",
                process=mock_proc,
            )

        yaml_verifier = MagicMock()
        yaml_verifier.verify_token = AsyncMock(return_value=None)
        yaml_verifier.config = MagicMock(tokens=[])

        key_store = MagicMock()
        key_store.by_token.return_value = None

        app = ep.build_app(yaml_verifier, key_store, pm)
        return TestClient(app), pm

    return _make


class TestAdminAuth:
    def test_no_admin_token_set_returns_404(self, app_factory):
        client, _ = app_factory(admin_token="")
        resp = client.get("/admin/status")
        assert resp.status_code == 404

    def test_missing_bearer_returns_401(self, app_factory):
        client, _ = app_factory(admin_token="supersecret")
        resp = client.get("/admin/status")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers

    def test_wrong_bearer_returns_401(self, app_factory):
        client, _ = app_factory(admin_token="supersecret")
        resp = client.get("/admin/status", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_correct_bearer_returns_200(self, app_factory):
        client, _ = app_factory(admin_token="supersecret")
        resp = client.get(
            "/admin/status", headers={"Authorization": "Bearer supersecret"}
        )
        assert resp.status_code == 200


class TestAdminStatus:
    def test_reports_worker_snapshot(self, app_factory):
        workers = {
            "user-a": {"pid": 111, "poll": None},   # alive
            "user-b": {"pid": 222, "poll": 0},      # dead
        }
        client, _ = app_factory(admin_token="t", workers=workers)
        resp = client.get("/admin/status", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["active"] == 1
        ids = {w["user_id"] for w in body["workers"]}
        assert ids == {"user-a", "user-b"}

    def test_reports_empty_when_no_workers(self, app_factory):
        client, _ = app_factory(admin_token="t")
        resp = client.get("/admin/status", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["active"] == 0
        assert body["workers"] == []


class TestAdminReloadUser:
    def test_reload_unknown_user_returns_ok_killed_false(self, app_factory):
        client, _ = app_factory(admin_token="t")
        resp = client.post(
            "/admin/reload-user/nobody", headers={"Authorization": "Bearer t"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"ok": True, "user_id": "nobody", "killed": False}

    def test_reload_rejects_path_traversal(self, app_factory):
        client, _ = app_factory(admin_token="t")
        # A URL-encoded ``..`` bypasses httpx's client-side path normalisation
        # and lands in ``path_params["user_id"]`` as the literal string ``..``.
        # The handler must reject it (defence-in-depth — the worker's filesystem
        # namespace means traversal wouldn't leak data, but we validate inputs
        # anyway).
        resp = client.post(
            "/admin/reload-user/%2E%2E",
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 400

    def test_reload_terminates_registered_worker(self, tmp_path: Path, monkeypatch):
        """End-to-end: admin POST → kill_worker → subprocess exits.

        Uses a real subprocess so we cover the signal path, not just a mock.
        """
        import sys

        from mcp_brain.isolation import entrypoint as ep
        from mcp_brain.isolation.manager import ProcessManager, WorkerInfo
        from starlette.testclient import TestClient

        monkeypatch.setattr(ep, "ADMIN_TOKEN", "t")

        pm = ProcessManager(
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )
        (tmp_path / "sockets").mkdir(parents=True, exist_ok=True)

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        socket_path = tmp_path / "sockets" / "eve.sock"
        socket_path.touch()
        pm._workers["eve"] = WorkerInfo(
            user_id="eve",
            pid=proc.pid,
            socket_path=socket_path,
            process=proc,
        )

        yaml_verifier = MagicMock()
        yaml_verifier.verify_token = AsyncMock(return_value=None)
        yaml_verifier.config = MagicMock(tokens=[])
        key_store = MagicMock()
        key_store.by_token.return_value = None

        client = TestClient(ep.build_app(yaml_verifier, key_store, pm))
        resp = client.post(
            "/admin/reload-user/eve", headers={"Authorization": "Bearer t"}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["user_id"] == "eve"
        assert body["killed"] is True
        assert "eve" not in pm._workers
        assert proc.poll() is not None
