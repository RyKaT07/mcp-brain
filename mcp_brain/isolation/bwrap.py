"""
Bubblewrap (bwrap) command builder for per-user sandboxed workers.

Constructs the full bwrap + python command list that spawns a sandboxed
mcp_brain worker process for a given user_id.  The caller (ProcessManager)
passes this to subprocess.Popen.

Sandbox design:
- Read-only bind for system paths: /usr, /lib, /lib64, /etc/resolv.conf, /etc/ssl
- Read-write bind for user knowledge dir → /data/knowledge inside sandbox
- Read-write bind for user state dir   → /data/state   inside sandbox
- Ephemeral /tmp, /dev, /proc
- PID namespace unshared, process dies with parent (--die-with-parent)
- Unix socket path is passed as --socket arg to the worker module
"""

from __future__ import annotations

from pathlib import Path


def build_bwrap_cmd(
    user_id: str,
    knowledge_base: Path,
    state_base: Path,
    socket_dir: Path,
    worker_module: str = "mcp_brain.worker",
) -> list[str]:
    """Return the full bwrap + python command to launch a sandboxed worker.

    Args:
        user_id:        Unique user identifier — used to derive per-user paths.
        knowledge_base: Root directory containing per-user knowledge dirs.
                        The user's dir is ``knowledge_base / user_id``.
        state_base:     Root directory containing per-user state dirs.
                        The user's dir is ``state_base / user_id``.
        socket_dir:     Directory where the worker's Unix socket will be created.
                        The socket path is ``socket_dir / user_id.sock``.
        worker_module:  Python module to run inside the sandbox (default:
                        ``mcp_brain.worker``).

    Returns:
        A list of strings suitable for ``subprocess.Popen(cmd, ...)``.
    """
    user_knowledge = knowledge_base / user_id
    user_state = state_base / user_id
    socket_path = socket_dir / f"{user_id}.sock"

    # Ensure host-side directories exist before bwrap tries to bind them.
    user_knowledge.mkdir(parents=True, exist_ok=True)
    user_state.mkdir(parents=True, exist_ok=True)
    socket_dir.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        "bwrap",
        # ── System read-only mounts ─────────────────────────────────────────
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
        "--ro-bind", "/etc/ssl", "/etc/ssl",
        # ── User data mounts (read-write) ───────────────────────────────────
        "--bind", str(user_knowledge), "/data/knowledge",
        "--bind", str(user_state), "/data/state",
        # ── Socket directory (read-write, needed to create the socket) ──────
        "--bind", str(socket_dir), str(socket_dir),
        # ── Ephemeral / virtual filesystems ────────────────────────────────
        "--tmpfs", "/tmp",
        "--dev", "/dev",
        "--proc", "/proc",
        # ── Namespace isolation ─────────────────────────────────────────────
        "--unshare-pid",
        "--die-with-parent",
        # ── Worker process ──────────────────────────────────────────────────
        "python3", "-m", worker_module,
        "--socket", str(socket_path),
    ]

    return cmd
