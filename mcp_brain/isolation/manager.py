"""
Process manager for per-user sandboxed mcp-brain workers.

Responsibilities:
- Spawn a bwrap-sandboxed worker process for each user on demand.
- Apply cgroups v2 resource limits to each worker.
- Reap idle workers whose last_activity exceeds idle_timeout seconds.
- Gracefully shut down all workers (SIGTERM → wait → SIGKILL).
- Track per-user last_activity timestamps for idle eviction.

Thread / async safety:
- All public methods are async and must be called from a single asyncio event
  loop.  Internal state is protected by asyncio.Lock so concurrent get_or_spawn
  calls for the same user_id serialize correctly.
- Worker stdout/stderr are inherited from the parent for easy Docker log
  aggregation.  This is intentional — the worker logs with structured JSON.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp_brain.isolation.bwrap import build_bwrap_cmd
from mcp_brain.isolation.cgroups import cleanup_cgroup, setup_cgroup

logger = logging.getLogger(__name__)

# How long (seconds) to wait for a worker's socket to appear after spawn.
# Index building happens async now, but leave headroom for large knowledge bases.
_SOCKET_READY_TIMEOUT = 60.0
# Poll interval when waiting for socket.
_SOCKET_POLL_INTERVAL = 0.05
# Grace period (seconds) between SIGTERM and SIGKILL during shutdown.
_SIGTERM_GRACE = 5.0


@dataclass
class WorkerInfo:
    user_id: str
    pid: int
    socket_path: Path
    process: subprocess.Popen
    last_activity: float = field(default_factory=time.monotonic)


class ProcessManager:
    """Manages the lifecycle of per-user bwrap worker processes.

    Args:
        knowledge_base: Root directory containing per-user knowledge dirs.
        state_base:     Root directory containing per-user state dirs.
        socket_dir:     Directory where Unix sockets for workers are created.
        idle_timeout:   Seconds of inactivity before a worker is reaped
                        (default: 600 = 10 minutes).
    """

    def __init__(
        self,
        knowledge_base: Path,
        state_base: Path,
        socket_dir: Path,
        idle_timeout: int = 600,
    ) -> None:
        self._knowledge_base = knowledge_base
        self._state_base = state_base
        self._socket_dir = socket_dir
        self._idle_timeout = idle_timeout

        self._workers: dict[str, WorkerInfo] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background idle-reaper task.  Call once after __init__."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reap_idle_loop(), name="worker-reaper"
            )

    async def get_or_spawn(self, user_id: str) -> WorkerInfo:
        """Return an existing live worker or spawn a new one.

        Concurrent calls for the same user_id serialize via an asyncio lock
        so only one spawn races to completion.
        """
        async with self._lock:
            info = self._workers.get(user_id)
            if info is not None and info.process.poll() is None:
                return info
            # Worker is missing or dead — (re)spawn.
            if info is not None:
                logger.warning(
                    "worker: user=%s pid=%d exited unexpectedly (rc=%s) — respawning",
                    user_id,
                    info.pid,
                    info.process.returncode,
                )
                self._cleanup_worker(user_id, info)
            info = await self._spawn_worker(user_id)
            self._workers[user_id] = info
            return info

    def touch(self, user_id: str) -> None:
        """Update last_activity for a worker to prevent idle eviction."""
        info = self._workers.get(user_id)
        if info is not None:
            info.last_activity = time.monotonic()

    async def shutdown(self) -> None:
        """Gracefully terminate all managed workers.

        Sends SIGTERM to every live worker and waits up to _SIGTERM_GRACE
        seconds.  Any process still alive after the grace period receives
        SIGKILL.
        """
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            if not self._workers:
                return
            logger.info("manager: shutting down %d worker(s)", len(self._workers))
            procs: list[tuple[str, WorkerInfo]] = list(self._workers.items())

            # SIGTERM all workers.
            for user_id, info in procs:
                _send_signal(info.process, signal.SIGTERM, user_id)

            # Wait up to grace period.
            deadline = time.monotonic() + _SIGTERM_GRACE
            for user_id, info in procs:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(info.process.wait), timeout=remaining
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            # SIGKILL stragglers.
            for user_id, info in procs:
                if info.process.poll() is None:
                    logger.warning(
                        "manager: worker user=%s pid=%d did not exit — sending SIGKILL",
                        user_id,
                        info.pid,
                    )
                    _send_signal(info.process, signal.SIGKILL, user_id)

            # Cleanup.
            for user_id, info in procs:
                self._cleanup_worker(user_id, info)
            self._workers.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _spawn_worker(self, user_id: str) -> WorkerInfo:
        """Spawn a bwrap-sandboxed worker and wait for its socket to appear.

        Raises:
            RuntimeError: if the socket does not appear within
                _SOCKET_READY_TIMEOUT seconds.
        """
        cmd = build_bwrap_cmd(
            user_id=user_id,
            knowledge_base=self._knowledge_base,
            state_base=self._state_base,
            socket_dir=self._socket_dir,
        )
        socket_path = self._socket_dir / f"{user_id}.sock"

        logger.info("manager: spawning worker user=%s cmd=%s", user_id, cmd[:4])
        proc = subprocess.Popen(
            cmd,
            # Inherit stdout/stderr for Docker log aggregation.
            stdout=None,
            stderr=None,
        )

        # Set up cgroup limits (best-effort — failure does not abort spawn).
        setup_cgroup(user_id, proc.pid)

        # Wait for the Unix socket to appear.
        deadline = time.monotonic() + _SOCKET_READY_TIMEOUT
        while not socket_path.exists():
            if proc.poll() is not None:
                raise RuntimeError(
                    f"worker for user={user_id} exited (rc={proc.returncode}) before socket appeared"
                )
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"timeout waiting for worker socket user={user_id} path={socket_path}"
                )
            await asyncio.sleep(_SOCKET_POLL_INTERVAL)

        info = WorkerInfo(
            user_id=user_id,
            pid=proc.pid,
            socket_path=socket_path,
            process=proc,
        )
        logger.info(
            "manager: worker ready user=%s pid=%d socket=%s",
            user_id,
            proc.pid,
            socket_path,
        )
        return info

    async def _reap_idle(self) -> None:
        """Kill workers that have been idle longer than idle_timeout."""
        now = time.monotonic()
        to_reap: list[str] = []

        async with self._lock:
            for user_id, info in list(self._workers.items()):
                if now - info.last_activity > self._idle_timeout:
                    to_reap.append(user_id)

            for user_id in to_reap:
                info = self._workers.get(user_id)
                if info is None:
                    continue
                logger.info(
                    "manager: reaping idle worker user=%s pid=%d (idle %.0fs)",
                    user_id,
                    info.pid,
                    now - info.last_activity,
                )
                _send_signal(info.process, signal.SIGTERM, user_id)
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(info.process.wait), timeout=_SIGTERM_GRACE
                    )
                except asyncio.TimeoutError:
                    _send_signal(info.process, signal.SIGKILL, user_id)
                self._cleanup_worker(user_id, info)
                del self._workers[user_id]

    async def _reap_idle_loop(self) -> None:
        """Background task: periodically evict idle workers."""
        # Check every minute; idle_timeout is typically much larger.
        interval = min(60, self._idle_timeout // 2 or 60)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._reap_idle()
            except Exception:
                logger.exception("manager: error in reaper loop")

    def _cleanup_worker(self, user_id: str, info: WorkerInfo) -> None:
        """Remove socket file and cgroup for an exited worker."""
        # Remove Unix socket.
        try:
            info.socket_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("manager: failed to remove socket %s: %s", info.socket_path, exc)
        # Remove cgroup (best-effort).
        cleanup_cgroup(user_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send_signal(proc: subprocess.Popen, sig: signal.Signals, user_id: str) -> None:
    """Send sig to proc, ignoring ProcessLookupError (already dead)."""
    try:
        os.kill(proc.pid, sig)
    except ProcessLookupError:
        pass
    except OSError as exc:
        logger.warning("manager: kill user=%s pid=%d sig=%s: %s", user_id, proc.pid, sig, exc)
