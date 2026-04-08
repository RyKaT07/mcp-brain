"""
Knowledge base tools — read and update project knowledge files.

Files are markdown with H2 sections. Updates are section-level (never full-file overwrite).
Git auto-commit after each write for history.
"""

import fcntl
import re
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import ALL, allowed_subscopes, require


def _git_commit(knowledge_dir: Path, filepath: Path, message: str):
    """Auto-commit a knowledge file change."""
    try:
        subprocess.run(["git", "add", str(filepath)], cwd=knowledge_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", message, "--", str(filepath)],
            cwd=knowledge_dir,
            capture_output=True,
        )
    except FileNotFoundError:
        pass  # git not installed — skip silently


def _parse_sections(content: str) -> dict[str, str]:
    """Parse markdown into {section_title: section_body} by H2 headers."""
    sections: dict[str, str] = {}
    current_title = "_preamble"
    current_lines: list[str] = []

    for line in content.splitlines(keepends=True):
        if line.startswith("## "):
            sections[current_title] = "".join(current_lines)
            current_title = line.strip("# \n")
            current_lines = []
        else:
            current_lines.append(line)

    sections[current_title] = "".join(current_lines)
    return sections


def _rebuild_markdown(sections: dict[str, str]) -> str:
    """Rebuild markdown from parsed sections dict."""
    parts: list[str] = []
    for title, body in sections.items():
        if title == "_preamble":
            if body.strip():
                parts.append(body)
        else:
            parts.append(f"## {title}\n{body}")
    return "\n".join(parts)


_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(name: str) -> str:
    return _SAFE_NAME_RE.sub("", name)


def _validate_scope_project(scope: str, project: str) -> str | None:
    """Return an error string if scope or project is invalid, else None.

    Rejects empty names (so callers can't escape the per-scope directory
    via strings that sanitize to "") and reserves underscore-prefixed
    scopes for server-internal configuration (like `_meta/write-policy.md`)
    that should only be edited via filesystem access, never via MCP tools.
    """
    safe_scope = _sanitize(scope)
    safe_project = _sanitize(project)
    if not safe_scope:
        return (
            f"Invalid scope {scope!r}: must contain at least one "
            f"[a-zA-Z0-9_-] character after sanitization."
        )
    if not safe_project:
        return (
            f"Invalid project {project!r}: must contain at least one "
            f"[a-zA-Z0-9_-] character after sanitization."
        )
    return None


def _validate_scope_writable(scope: str) -> str | None:
    """Return an error if `scope` is reserved (underscore-prefixed), else None.

    Read access to `_meta/` is fine (the policy is already visible via MCP
    instructions) but writes must go through the filesystem so a
    compromised agent cannot rewrite its own discipline rules.
    """
    if _sanitize(scope).startswith("_"):
        return (
            f"Refused: scope {scope!r} is reserved for server-internal "
            f"configuration. Edit files under knowledge/_meta/ directly "
            f"via SSH; not through knowledge_update."
        )
    return None


def _resolve_file(knowledge_dir: Path, scope: str, project: str) -> Path:
    """Resolve knowledge file path: knowledge/{scope}/{project}.md.

    Callers MUST run _validate_scope_project first — this function trusts
    its inputs and only applies the character-class sanitization as a
    second defense.
    """
    return knowledge_dir / _sanitize(scope) / f"{_sanitize(project)}.md"


def register_knowledge_tools(mcp: FastMCP, knowledge_dir: Path):

    @mcp.tool()
    def knowledge_read(scope: str, project: str, section: str | None = None) -> str:
        """Read a knowledge file or a specific section.

        Args:
            scope: Category — 'work', 'school', or 'homelab'
            project: Project/topic name (filename without .md)
            section: Optional H2 section title. If omitted, returns full file.
        """
        try:
            require(f"knowledge:read:{scope}")
        except PermissionDenied as e:
            return str(e)

        err = _validate_scope_project(scope, project)
        if err:
            return err

        filepath = _resolve_file(knowledge_dir, scope, project)
        if not filepath.exists():
            return f"No knowledge file found: {scope}/{project}"

        content = filepath.read_text(encoding="utf-8")

        if section is None:
            return content

        sections = _parse_sections(content)
        if section in sections:
            return f"## {section}\n{sections[section]}"
        return f"Section '{section}' not found. Available: {', '.join(s for s in sections if s != '_preamble')}"

    @mcp.tool()
    def knowledge_update(scope: str, project: str, section: str, content: str) -> str:
        """Update (or create) a specific section in a knowledge file.

        This replaces only the target H2 section, leaving all others untouched.
        Creates the file if it doesn't exist.

        Args:
            scope: Category — 'work', 'school', or 'homelab'
            project: Project/topic name
            section: H2 section title to update (e.g. 'architecture', 'current_tasks')
            content: New markdown content for that section (without the ## header)
        """
        try:
            require(f"knowledge:write:{scope}")
        except PermissionDenied as e:
            return str(e)

        err = _validate_scope_project(scope, project)
        if err:
            return err

        err = _validate_scope_writable(scope)
        if err:
            return err

        filepath = _resolve_file(knowledge_dir, scope, project)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # File-level lock to prevent concurrent writes
        lock_path = filepath.with_suffix(".lock")
        lock_path.touch(exist_ok=True)

        with open(lock_path, "r") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if filepath.exists():
                    existing = filepath.read_text(encoding="utf-8")
                    sections = _parse_sections(existing)
                else:
                    sections = {"_preamble": f"# {project}\n\n"}

                # Ensure content ends with newline
                if not content.endswith("\n"):
                    content += "\n"

                sections[section] = content
                filepath.write_text(_rebuild_markdown(sections), encoding="utf-8")

                _git_commit(knowledge_dir, filepath, f"update {scope}/{project} § {section}")

                return f"Updated {scope}/{project} § {section}"
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    @mcp.tool()
    def knowledge_list(scope: str | None = None) -> str:
        """List available knowledge files.

        Args:
            scope: Optional filter — 'work', 'school', 'homelab'. If omitted, lists all.
        """
        if scope is not None:
            try:
                require(f"knowledge:read:{scope}")
            except PermissionDenied as e:
                return str(e)
            search_dirs = [knowledge_dir / scope]
        else:
            # Default listing hides internal scopes (any `_foo` dir) and the
            # inbox staging area. Callers who genuinely want those must pass
            # `scope="_meta"` (or similar) explicitly and have the matching
            # read grant.
            allowed = allowed_subscopes("knowledge:read")
            search_dirs = [
                d
                for d in knowledge_dir.iterdir()
                if d.is_dir()
                and d.name not in ("inbox", ".git")
                and not d.name.startswith("_")
                and (allowed is ALL or d.name in allowed)
            ]

        results: list[str] = []
        for d in search_dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob("*.md")):
                results.append(f"{d.name}/{f.stem}")

        return "\n".join(results) if results else "No knowledge files found."

    @mcp.tool()
    def knowledge_undo(steps: int = 1) -> str:
        """Revert the most recent knowledge commit(s).

        Non-destructive: uses `git revert` to create new commits that undo
        the target commits. History is preserved and can itself be reverted,
        so this is always safe. Call this when the user says "cofnij",
        "usun to", "undo", "don't save that", "delete the last save" or any
        equivalent request to back out a recent write.

        Requires a full knowledge:write:* scope — partial-scope tokens cannot
        undo, because a revert may need to touch files outside their scope.

        Args:
            steps: Number of most recent commits to revert (newest first).
                   Default 1. Hard-capped at 10 as a safety limit; if the
                   user wants to undo further, ssh into the host.

        Returns:
            Summary of which commits were reverted, or an error string.
        """
        try:
            require("knowledge:write:*")
        except PermissionDenied as e:
            return f"{e}. knowledge_undo requires knowledge:write:* (full write scope)."

        if steps < 1:
            return "Error: steps must be >= 1"
        if steps > 10:
            return "Error: steps must be <= 10 (safety limit — use ssh for deeper rollbacks)"

        # 1. How many commits exist total? We refuse to wipe the init commit.
        try:
            count_out = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=knowledge_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            total = int(count_out.stdout.strip())
        except FileNotFoundError:
            return "Error: git not installed inside the container"
        except subprocess.CalledProcessError as e:
            return f"Git error (rev-list): {e.stderr.strip() or e.stdout.strip()}"
        except ValueError:
            return "Error: could not parse commit count from git"

        if total - steps < 1:
            return (
                f"Refused: only {total} commit(s) exist (including init). "
                f"Reverting {steps} would leave history empty."
            )

        # 2. Fetch the SHAs + subjects of the commits we are about to revert.
        #    Newest first so we revert in reverse-chronological order.
        try:
            log_out = subprocess.run(
                ["git", "log", "--format=%H %s", f"-{steps}"],
                cwd=knowledge_dir,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            return f"Git error (log): {e.stderr.strip() or e.stdout.strip()}"

        lines = [line for line in log_out.stdout.strip().split("\n") if line]
        if not lines:
            return "No commits to revert."

        # 3. Revert by explicit SHA (safe even though HEAD moves underneath us).
        reverted: list[str] = []
        for line in lines:
            sha, _, subject = line.partition(" ")
            try:
                subprocess.run(
                    ["git", "revert", "--no-edit", sha],
                    cwd=knowledge_dir,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                reverted.append(f"  {sha[:7]} {subject}")
            except subprocess.CalledProcessError as e:
                # Partial-success report: what we managed, and where we stopped.
                msg = e.stderr.strip() or e.stdout.strip() or "unknown error"
                already = "\n".join(reverted) if reverted else "  (none)"
                return (
                    f"Partial revert. Failed on {sha[:7]} ({subject}): {msg}\n"
                    f"Successfully reverted before failure:\n{already}\n"
                    f"Resolve the conflict manually inside the container "
                    f"(`cd /data/knowledge && git status`)."
                )

        return (
            f"Reverted {len(reverted)} commit(s):\n"
            + "\n".join(reverted)
            + "\n\nHistory preserved — each revert is itself a new commit "
            "and can be undone again with `git revert HEAD`."
        )
