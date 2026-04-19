"""
cgroups v2 helpers for per-user worker resource limits.

Creates a cgroup at /sys/fs/cgroup/mcp-brain/user-{user_id}/, writes the
configured limits, and moves the worker PID into the group.  Cleanup removes
the cgroup directory after the worker exits.

Requirements:
- Running on a host with cgroups v2 (unified hierarchy) mounted at
  /sys/fs/cgroup (standard on Debian 11+ / Ubuntu 21.10+).
- The process has write access to /sys/fs/cgroup/mcp-brain/ (either running
  as root, or the cgroup subtree is delegated to the user via systemd).

Errors during setup are logged as warnings and do not prevent the worker from
starting — resource limits are best-effort.  Errors during cleanup are also
logged but do not propagate.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CGROUP_ROOT = Path(os.getenv("MCP_CGROUP_ROOT", "/run/cgroup/mcp-brain"))


def _cgroup_path(user_id: str) -> Path:
    return _CGROUP_ROOT / f"user-{user_id}"


def setup_cgroup(
    user_id: str,
    pid: int,
    memory_max: int = 512 * 1024 * 1024,
    cpu_pct: int = 25,
    pids_max: int = 100,
) -> Path:
    """Create a cgroup for user_id, apply resource limits, and move pid into it.

    Args:
        user_id:    User identifier — becomes part of the cgroup directory name.
        pid:        PID of the worker process to add to the cgroup.
        memory_max: Maximum resident memory in bytes (default: 512 MiB).
        cpu_pct:    CPU quota as a percentage of one core (default: 25%).
                    Implemented via cpu.max: period 100000 µs, quota = period
                    * cpu_pct / 100.
        pids_max:   Maximum number of PIDs allowed in the cgroup (default: 100).

    Returns:
        The Path to the created cgroup directory.

    Raises:
        Does not raise — logs warnings on failure so the caller can proceed
        without cgroup enforcement.
    """
    cg = _cgroup_path(user_id)
    try:
        cg.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cgroup: failed to create %s: %s", cg, exc)
        return cg

    # ── memory.max ──────────────────────────────────────────────────────────
    _write_cgroup(cg / "memory.max", str(memory_max))

    # ── cpu.max (quota period) ───────────────────────────────────────────────
    # Format: "{quota} {period}\n"  — quota = period * cpu_pct / 100
    period = 100_000  # µs
    quota = int(period * cpu_pct / 100)
    _write_cgroup(cg / "cpu.max", f"{quota} {period}")

    # ── pids.max ─────────────────────────────────────────────────────────────
    _write_cgroup(cg / "pids.max", str(pids_max))

    # ── Add the worker PID ───────────────────────────────────────────────────
    _write_cgroup(cg / "cgroup.procs", str(pid))

    logger.info(
        "cgroup: user=%s pid=%d memory_max=%d cpu_pct=%d pids_max=%d path=%s",
        user_id,
        pid,
        memory_max,
        cpu_pct,
        pids_max,
        cg,
    )
    return cg


def cleanup_cgroup(user_id: str) -> None:
    """Remove the cgroup directory for user_id after the worker exits.

    The cgroup must be empty (no live PIDs) before the kernel allows rmdir.
    If the directory does not exist this is a no-op.
    """
    cg = _cgroup_path(user_id)
    if not cg.exists():
        return
    try:
        os.rmdir(cg)
        logger.debug("cgroup: removed %s", cg)
    except OSError as exc:
        logger.warning("cgroup: failed to remove %s: %s", cg, exc)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _write_cgroup(path: Path, value: str) -> None:
    """Write a string value to a cgroup control file, logging on failure."""
    try:
        path.write_text(value + "\n", encoding="ascii")
    except OSError as exc:
        logger.warning("cgroup: write %s=%r failed: %s", path.name, value, exc)
