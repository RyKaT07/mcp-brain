"""Tests for the isolation module (bwrap command builder, cgroups, manager)."""

from pathlib import Path


class TestBwrapCommandBuilder:
    """Tests for mcp_brain.isolation.bwrap.build_bwrap_cmd."""

    def test_builds_valid_command(self, tmp_path: Path):
        from mcp_brain.isolation.bwrap import build_bwrap_cmd

        cmd = build_bwrap_cmd(
            user_id="alice",
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )

        assert cmd[0] == "bwrap"
        assert "python3" in cmd
        assert "-m" in cmd
        assert "mcp_brain.worker" in cmd
        assert "--socket" in cmd

    def test_creates_host_directories(self, tmp_path: Path):
        from mcp_brain.isolation.bwrap import build_bwrap_cmd

        knowledge_base = tmp_path / "knowledge"
        state_base = tmp_path / "state"
        socket_dir = tmp_path / "sockets"

        build_bwrap_cmd(
            user_id="bob",
            knowledge_base=knowledge_base,
            state_base=state_base,
            socket_dir=socket_dir,
        )

        assert (knowledge_base / "bob").is_dir()
        assert (state_base / "bob").is_dir()
        assert socket_dir.is_dir()

    def test_user_knowledge_mounted_as_data_knowledge(self, tmp_path: Path):
        from mcp_brain.isolation.bwrap import build_bwrap_cmd

        cmd = build_bwrap_cmd(
            user_id="carol",
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )

        # Find --bind pairs and verify user knowledge dir → /data/knowledge
        bind_pairs = []
        for i, arg in enumerate(cmd):
            if arg == "--bind" and i + 2 < len(cmd):
                bind_pairs.append((cmd[i + 1], cmd[i + 2]))

        user_knowledge = str(tmp_path / "knowledge" / "carol")
        assert (user_knowledge, "/data/knowledge") in bind_pairs

    def test_die_with_parent(self, tmp_path: Path):
        from mcp_brain.isolation.bwrap import build_bwrap_cmd

        cmd = build_bwrap_cmd(
            user_id="dave",
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )

        assert "--die-with-parent" in cmd

    def test_socket_path_in_command(self, tmp_path: Path):
        from mcp_brain.isolation.bwrap import build_bwrap_cmd

        cmd = build_bwrap_cmd(
            user_id="eve",
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )

        socket_idx = cmd.index("--socket")
        assert cmd[socket_idx + 1] == str(tmp_path / "sockets" / "eve.sock")

    def test_custom_worker_module(self, tmp_path: Path):
        from mcp_brain.isolation.bwrap import build_bwrap_cmd

        cmd = build_bwrap_cmd(
            user_id="frank",
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
            worker_module="custom.worker",
        )

        assert "custom.worker" in cmd

    def test_system_paths_readonly(self, tmp_path: Path):
        from mcp_brain.isolation.bwrap import build_bwrap_cmd

        cmd = build_bwrap_cmd(
            user_id="grace",
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )

        ro_pairs = []
        for i, arg in enumerate(cmd):
            if arg == "--ro-bind" and i + 2 < len(cmd):
                ro_pairs.append((cmd[i + 1], cmd[i + 2]))

        # System paths should be read-only
        assert ("/usr", "/usr") in ro_pairs
        assert ("/lib", "/lib") in ro_pairs


class TestCgroupsHelper:
    """Tests for mcp_brain.isolation.cgroups — unit tests only.

    We cannot test actual cgroup creation without root. These tests verify
    the path computation and error handling.
    """

    def test_cgroup_path_format(self):
        from mcp_brain.isolation.cgroups import _cgroup_path

        p = _cgroup_path("alice")
        assert p == Path("/sys/fs/cgroup/mcp-brain/user-alice")

    def test_cleanup_nonexistent_is_noop(self):
        from mcp_brain.isolation.cgroups import cleanup_cgroup

        # Should not raise
        cleanup_cgroup("nonexistent-user-id-for-test")


class TestWorkerInfo:
    """Tests for the WorkerInfo dataclass."""

    def test_dataclass_fields(self):
        import subprocess
        import time
        from unittest.mock import MagicMock

        from mcp_brain.isolation.manager import WorkerInfo

        mock_proc = MagicMock(spec=subprocess.Popen)
        before = time.monotonic()

        info = WorkerInfo(
            user_id="test",
            pid=12345,
            socket_path=Path("/tmp/test.sock"),
            process=mock_proc,
        )

        assert info.user_id == "test"
        assert info.pid == 12345
        assert info.socket_path == Path("/tmp/test.sock")
        assert info.process is mock_proc
        assert info.last_activity >= before


# ── ProcessManager live-subprocess tests ─────────────────────────────────────
#
# kill_worker() sends real signals to real PIDs, so we can't fake it with just
# a MagicMock — we spawn a short-lived Python subprocess, register it with the
# manager, then assert that kill_worker terminates it cleanly.


def _spawn_sleep_proc(tmp_path: Path):
    """Spawn a Python subprocess that sleeps until signalled."""
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


class TestKillWorker:
    """Tests for ProcessManager.kill_worker — runtime-triggered worker reload."""

    async def test_kill_worker_terminates_process(self, tmp_path: Path):
        from mcp_brain.isolation.manager import ProcessManager, WorkerInfo

        pm = ProcessManager(
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )
        (tmp_path / "sockets").mkdir(parents=True, exist_ok=True)

        proc = _spawn_sleep_proc(tmp_path)
        socket_path = tmp_path / "sockets" / "alice.sock"
        socket_path.touch()  # _cleanup_worker unlinks this; ensure it exists

        pm._workers["alice"] = WorkerInfo(
            user_id="alice",
            pid=proc.pid,
            socket_path=socket_path,
            process=proc,
        )

        killed = await pm.kill_worker("alice")

        assert killed is True
        assert "alice" not in pm._workers
        # Subprocess actually exited.
        assert proc.poll() is not None
        # Socket file cleaned up.
        assert not socket_path.exists()

    async def test_kill_worker_unknown_user_returns_false(self, tmp_path: Path):
        from mcp_brain.isolation.manager import ProcessManager

        pm = ProcessManager(
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )
        killed = await pm.kill_worker("no-such-user")
        assert killed is False

    async def test_kill_worker_already_dead_cleans_up(self, tmp_path: Path):
        """If the subprocess already exited, we still remove it from _workers."""
        import subprocess
        import sys

        from mcp_brain.isolation.manager import ProcessManager, WorkerInfo

        pm = ProcessManager(
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )
        (tmp_path / "sockets").mkdir(parents=True, exist_ok=True)

        # Spawn a process that exits immediately.
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()

        socket_path = tmp_path / "sockets" / "zombie.sock"
        socket_path.touch()

        pm._workers["zombie"] = WorkerInfo(
            user_id="zombie",
            pid=proc.pid,
            socket_path=socket_path,
            process=proc,
        )

        killed = await pm.kill_worker("zombie")
        # No live worker to kill, so returns False — but still cleans up.
        assert killed is False
        assert "zombie" not in pm._workers
        assert not socket_path.exists()


class TestListWorkers:
    """Tests for ProcessManager.list_workers — snapshot used by /admin/status."""

    def test_empty_registry(self, tmp_path: Path):
        from mcp_brain.isolation.manager import ProcessManager

        pm = ProcessManager(
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )
        assert pm.list_workers() == []

    def test_snapshot_reports_live_and_dead(self, tmp_path: Path):
        import subprocess
        from unittest.mock import MagicMock

        from mcp_brain.isolation.manager import ProcessManager, WorkerInfo

        pm = ProcessManager(
            knowledge_base=tmp_path / "knowledge",
            state_base=tmp_path / "state",
            socket_dir=tmp_path / "sockets",
        )

        live = MagicMock(spec=subprocess.Popen)
        live.poll.return_value = None  # alive
        dead = MagicMock(spec=subprocess.Popen)
        dead.poll.return_value = 0  # exited

        pm._workers["alive-user"] = WorkerInfo(
            user_id="alive-user",
            pid=111,
            socket_path=tmp_path / "sockets" / "alive.sock",
            process=live,
        )
        pm._workers["dead-user"] = WorkerInfo(
            user_id="dead-user",
            pid=222,
            socket_path=tmp_path / "sockets" / "dead.sock",
            process=dead,
        )

        snapshot = pm.list_workers()
        by_id = {w["user_id"]: w for w in snapshot}

        assert by_id["alive-user"]["alive"] is True
        assert by_id["alive-user"]["pid"] == 111
        assert isinstance(by_id["alive-user"]["idle_seconds"], float)
        assert by_id["dead-user"]["alive"] is False
        assert by_id["dead-user"]["pid"] == 222
