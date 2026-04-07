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


def _resolve_file(knowledge_dir: Path, scope: str, project: str) -> Path:
    """Resolve knowledge file path: knowledge/{scope}/{project}.md"""
    safe_scope = re.sub(r"[^a-zA-Z0-9_-]", "", scope)
    safe_project = re.sub(r"[^a-zA-Z0-9_-]", "", project)
    return knowledge_dir / safe_scope / f"{safe_project}.md"


def register_knowledge_tools(mcp: FastMCP, knowledge_dir: Path):

    @mcp.tool()
    def knowledge_read(scope: str, project: str, section: str | None = None) -> str:
        """Read a knowledge file or a specific section.

        Args:
            scope: Category — 'work', 'school', or 'homelab'
            project: Project/topic name (filename without .md)
            section: Optional H2 section title. If omitted, returns full file.
        """
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
        results: list[str] = []
        search_dirs = (
            [knowledge_dir / scope] if scope else
            [d for d in knowledge_dir.iterdir() if d.is_dir() and d.name not in ("inbox", ".git")]
        )

        for d in search_dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob("*.md")):
                results.append(f"{d.name}/{f.stem}")

        return "\n".join(results) if results else "No knowledge files found."
