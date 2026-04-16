"""
knowledge_maintain — automated vault hygiene tool.

Audits the knowledge base for:
  1. Staleness — files not updated in N days (git-based, same as knowledge_freshness)
  2. Broken cross-references — `scope/project` backtick links pointing to missing files
  3. meta.yaml sync — mismatch between `projects:` keys and on-disk scope directories

Returns a structured markdown report. Read-only; never modifies any files.
"""

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import (
    ALL,
    allowed_subscopes,
    get_effective_knowledge_dir,
    meter_call,
    require,
)

logger = logging.getLogger(__name__)

# Matches `scope/project` backtick cross-references in markdown content.
_XREF_RE = re.compile(r"`([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)`")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def register_maintain_tools(mcp: FastMCP, knowledge_dir: Path) -> None:
    """Register knowledge_maintain tool on the MCP server."""

    @mcp.tool()
    def knowledge_maintain(
        scope: str | None = None,
        stale_days: int = 90,
    ) -> str:
        """Audit the knowledge vault and return a hygiene report.

        Checks three things:
        1. **Staleness** — knowledge files not updated in `stale_days` days
           (uses git history, same source as knowledge_freshness).
        2. **Broken cross-references** — backtick `scope/project` links inside
           any file that point to a file that does not exist on disk.
        3. **meta.yaml sync** — compares the `projects:` keys declared in
           meta.yaml against the actual scope directories on disk, flagging
           any mismatches in either direction.

        Returns a structured markdown report with findings per category.
        Call this periodically for vault hygiene or after a bulk reorganization.

        Args:
            scope: Optional scope filter — e.g. 'work', 'school'. If omitted,
                   audits all readable scopes.
            stale_days: Flag files not updated in this many days. Default 90.
        """
        meter_call("knowledge_maintain")
        effective_dir = get_effective_knowledge_dir(knowledge_dir)
        allowed = allowed_subscopes("knowledge:read")

        if allowed is not ALL and scope and scope not in allowed:
            return f"Permission denied: no read access to scope '{scope}'."

        # Determine which scope directories to scan.
        if scope:
            safe_scope = _SAFE_NAME_RE.sub("", scope)
            if not safe_scope:
                return f"Invalid scope {scope!r}: no valid characters after sanitization."
            search_dirs = [effective_dir / safe_scope]
        else:
            if not effective_dir.exists():
                return "No knowledge files found."
            search_dirs = sorted(
                d
                for d in effective_dir.iterdir()
                if d.is_dir()
                and not d.name.startswith((".", "_"))
                and d.name not in ("inbox", ".git")
            )
            if allowed is not ALL:
                search_dirs = [d for d in search_dirs if d.name in allowed]

        now = datetime.now(timezone.utc)

        # ----------------------------------------------------------------
        # Pass 1: collect all known files + staleness data in one scan.
        # ----------------------------------------------------------------
        all_known_files: set[str] = set()  # "scope/project"
        stale_files: list[tuple[str, int]] = []  # (key, days_ago)

        for scope_dir in search_dirs:
            if not scope_dir.is_dir():
                continue
            for md_file in sorted(scope_dir.glob("*.md")):
                key = f"{scope_dir.name}/{md_file.stem}"
                all_known_files.add(key)

                try:
                    result = subprocess.run(
                        ["git", "log", "-1", "--format=%aI", "--", str(md_file)],
                        cwd=effective_dir,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    date_str = result.stdout.strip()
                    if date_str:
                        last_modified = datetime.fromisoformat(date_str)
                        days_ago = (now - last_modified).days
                        if days_ago >= stale_days:
                            stale_files.append((key, days_ago))
                    # Untracked (no git history) — skip staleness flag.
                except (FileNotFoundError, subprocess.CalledProcessError):
                    pass  # git not available or failed — skip staleness for this file

        stale_files.sort(key=lambda x: -x[1])  # worst first

        # ----------------------------------------------------------------
        # Pass 2: cross-reference check.
        # ----------------------------------------------------------------
        broken_refs: list[tuple[str, str]] = []  # (source_key, broken_ref)

        for scope_dir in search_dirs:
            if not scope_dir.is_dir():
                continue
            for md_file in sorted(scope_dir.glob("*.md")):
                source_key = f"{scope_dir.name}/{md_file.stem}"
                try:
                    content = md_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                for ref in sorted(set(_XREF_RE.findall(content))):
                    if ref not in all_known_files:
                        broken_refs.append((source_key, ref))

        broken_refs.sort()

        # ----------------------------------------------------------------
        # Pass 3: meta.yaml sync.
        # ----------------------------------------------------------------
        meta_issues: list[str] = []
        meta_path = effective_dir / "meta.yaml"

        if not meta_path.exists():
            meta_issues.append("  meta.yaml not found — cannot check project sync.")
        else:
            try:
                require("meta:read")
                try:
                    raw = meta_path.read_text(encoding="utf-8")
                    meta_data = yaml.safe_load(raw) or {}
                except (OSError, yaml.YAMLError) as exc:
                    meta_data = {}
                    meta_issues.append(f"  Error reading meta.yaml: {exc}")

                if meta_data:
                    meta_scopes: set[str] = set(meta_data.get("projects", {}).keys())

                    # Actual non-reserved scope directories on disk.
                    actual_scopes: set[str] = set()
                    if effective_dir.exists():
                        actual_scopes = {
                            d.name
                            for d in effective_dir.iterdir()
                            if d.is_dir()
                            and not d.name.startswith((".", "_"))
                            and d.name not in ("inbox", ".git")
                        }

                    # When a scope filter is active, narrow both sets.
                    if scope:
                        safe = _SAFE_NAME_RE.sub("", scope)
                        actual_scopes &= {safe}
                        meta_scopes &= {safe}

                    for s in sorted(meta_scopes - actual_scopes):
                        meta_issues.append(
                            f"  - `{s}` declared in meta.yaml `projects:` "
                            f"but no `{s}/` directory exists on disk."
                        )
                    for s in sorted(actual_scopes - meta_scopes):
                        meta_issues.append(
                            f"  - `{s}/` directory exists on disk "
                            f"but is not declared in meta.yaml `projects:`."
                        )

            except PermissionDenied:
                meta_issues.append(
                    "  (meta:read permission required to check meta.yaml sync — skipped)"
                )

        # ----------------------------------------------------------------
        # Build markdown report.
        # ----------------------------------------------------------------
        lines: list[str] = ["# Knowledge Vault Maintenance Report", ""]

        # --- Staleness ---
        lines.append(f"## 1. Staleness (>{stale_days}d without update)")
        if stale_files:
            for key, days in stale_files:
                lines.append(f"  - `{key}` — {days}d ago")
        else:
            lines.append(f"  ✅ No files older than {stale_days} days.")
        lines.append("")

        # --- Broken cross-refs ---
        lines.append("## 2. Broken cross-references")
        if broken_refs:
            for source, ref in broken_refs:
                lines.append(f"  - `{source}` → `{ref}` (target not found)")
        else:
            lines.append("  ✅ No broken cross-references.")
        lines.append("")

        # --- meta.yaml sync ---
        lines.append("## 3. meta.yaml sync")
        if meta_issues:
            lines.extend(meta_issues)
        else:
            lines.append("  ✅ meta.yaml `projects:` matches on-disk scopes.")
        lines.append("")

        # --- Summary ---
        total = len(stale_files) + len(broken_refs) + len(
            [i for i in meta_issues if i.strip().startswith("-")]
        )
        if total == 0:
            lines.append("**Vault is healthy — no issues found.**")
        else:
            lines.append(
                f"**{total} issue(s) found.** "
                f"Review stale files with `knowledge_read`, fix broken refs with "
                f"`knowledge_update` or `knowledge_delete`, and update meta.yaml "
                f"with `meta_update` if needed."
            )

        return "\n".join(lines)
