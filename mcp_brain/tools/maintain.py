"""
knowledge_maintain — automated vault hygiene tool with interactive session.

Provides two modes:
  1. Audit (read-only): knowledge_maintain(scope, stale_days) — unchanged from v1.
  2. Interactive session: maintain_start / maintain_answer / maintain_confirm / maintain_skip
     — start a guided session that asks targeted questions about stale files and
     applies user-confirmed updates.

Audit checks:
  1. Staleness — files not updated in N days (git-based)
  2. Broken cross-references — `scope/project` backtick links pointing to missing files
  3. meta.yaml sync — mismatch between `projects:` keys and on-disk scope directories

Session flow:
  maintain_start  → creates session, returns first question
  maintain_answer → LLM drafts a proposed change, returns diff for review
  maintain_confirm → applies the change and advances to next question
  maintain_skip   → skips current question, advances to next
"""

import difflib
import fcntl
import json
import logging
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import (
    ALL,
    allowed_subscopes,
    get_effective_knowledge_dir,
    meter_call,
    require,
    require_path_within,
)

logger = logging.getLogger(__name__)

# Matches `scope/project` backtick cross-references in markdown content.
_XREF_RE = re.compile(r"`([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)`")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")

# Content hint regex patterns
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?)\b")
_TODO_RE = re.compile(r"\b(TODO|FIXME)\b[^\n]*", re.IGNORECASE)

SESSION_FILENAME = ".maintain_session.json"
MAX_QUESTIONS = 10
BATCH_SIZE = 5


# ---------------------------------------------------------------------------
# Session helpers


def _get_session_path(knowledge_dir: Path) -> Path:
    return knowledge_dir / SESSION_FILENAME


def _load_session(session_path: Path) -> dict | None:
    if not session_path.exists():
        return None
    try:
        return json.loads(session_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_session(session_path: Path, session: dict) -> None:
    session_path.write_text(
        json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _delete_session(session_path: Path) -> None:
    try:
        session_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Content hint extraction (no LLM)


def extract_content_hints(content: str) -> list[dict]:
    """Scan markdown content for dates, versions, and TODOs.

    Returns list of {type, value, line_number} dicts. Dates are only
    flagged when they are more than 30 days old.
    """
    hints: list[dict] = []
    now = datetime.now(timezone.utc)

    for line_num, line in enumerate(content.splitlines(), 1):
        # ISO dates older than 30 days
        for m in _DATE_RE.finditer(line):
            try:
                dt = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                days_ago = (now - dt).days
                if days_ago > 30:
                    hints.append(
                        {
                            "type": "date",
                            "value": m.group(1),
                            "line_number": line_num,
                            "days_ago": days_ago,
                        }
                    )
            except ValueError:
                pass

        # Version strings
        for m in _VERSION_RE.finditer(line):
            hints.append(
                {"type": "version", "value": m.group(0), "line_number": line_num}
            )

        # TODO / FIXME markers
        for m in _TODO_RE.finditer(line):
            hints.append(
                {
                    "type": "todo",
                    "value": m.group(0).strip()[:100],
                    "line_number": line_num,
                }
            )

    return hints


# ---------------------------------------------------------------------------
# Question generation


def _generate_questions_cheap(stale_files: list[dict]) -> list[dict]:
    """Generate simple questions without LLM, based on content hints."""
    questions: list[dict] = []
    for f in stale_files:
        hints = f.get("hints", [])
        if hints:
            hint_text = "; ".join(
                f"{h['type']}: {h['value']} (line {h['line_number']})"
                for h in hints[:5]
            )
            question = (
                f"Content hints found: {hint_text}. "
                f"Is this information still current? What (if anything) should be updated?"
            )
        else:
            question = (
                f"This file hasn't been updated in {f['days_stale']} days. "
                f"Is the information still current?"
            )
        questions.append(
            {"file": f["path"], "question": question, "hint_context": hints}
        )
    return questions


def _generate_questions_llm(stale_files: list[dict]) -> list[dict]:
    """Generate targeted questions via LLM, batching up to BATCH_SIZE files per call.

    Falls back to cheap mode if the anthropic package is not installed or if
    ANTHROPIC_API_KEY is not set.
    """
    try:
        import anthropic
    except ImportError:
        logger.warning(
            "anthropic package not installed — falling back to cheap question generation"
        )
        return _generate_questions_cheap(stale_files)

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    questions: list[dict] = []

    for i in range(0, len(stale_files), BATCH_SIZE):
        batch = stale_files[i : i + BATCH_SIZE]
        remaining = MAX_QUESTIONS - len(questions)
        if remaining <= 0:
            break

        # Build concise file summaries for this batch
        file_blocks: list[str] = []
        for f in batch:
            hints_line = ""
            if f.get("hints"):
                hints_line = "\n  Hints: " + ", ".join(
                    f"{h['type']}={h['value']}" for h in f["hints"][:5]
                )
            preview = f.get("content", "")[:600]
            file_blocks.append(
                f"File: {f['path']} (stale {f['days_stale']}d){hints_line}\n"
                f"Content preview:\n{preview}"
            )

        user_msg = (
            "Generate one targeted question per knowledge file to verify whether "
            "its content is still current. Focus on specific dates, version strings, "
            "or TODO items from the hints. Return a JSON array:\n"
            '[{"file": "scope/name", "question": "...", "hint_context": "..."}]\n\n'
            + "\n\n---\n\n".join(file_blocks)
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=(
                    "You are a knowledge-base assistant. Generate targeted, specific "
                    "questions to verify staleness of markdown knowledge files. "
                    "Return ONLY valid JSON — no prose, no markdown fences."
                ),
                messages=[{"role": "user", "content": user_msg}],
            )
            text = response.content[0].text.strip()
            # Tolerate JSON wrapped in a markdown code block
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            json_match = re.search(r"\[.*\]", text, re.DOTALL)
            if json_match:
                batch_questions = json.loads(json_match.group(0))
                questions.extend(batch_questions[:remaining])
            else:
                raise ValueError("No JSON array found in LLM response")
        except Exception as e:
            logger.warning("LLM question generation failed (%s) — falling back", e)
            questions.extend(_generate_questions_cheap(batch)[:remaining])

    return questions[:MAX_QUESTIONS]


# ---------------------------------------------------------------------------
# Session display helpers


def _format_next_question(session: dict) -> str:
    idx = session["current_question_index"]
    questions = session["questions"]
    if idx >= len(questions):
        return _format_done_summary(session)
    q = questions[idx]
    total = len(questions)
    return (
        f"**Question {idx + 1}/{total}** — `{q['file']}`\n\n"
        f"{q['question']}\n\n"
        f"_Session ID: {session['session_id']}_"
    )


def _format_done_summary(session: dict) -> str:
    questions = session["questions"]
    confirmed = sum(1 for q in questions if q["status"] == "confirmed")
    skipped = sum(1 for q in questions if q["status"] == "skipped")
    pending = sum(1 for q in questions if q["status"] == "pending")
    return (
        f"**Maintain session complete.**\n\n"
        f"- Updated: {confirmed} file(s)\n"
        f"- Skipped: {skipped} question(s)\n"
        f"- Not reached: {pending} question(s)\n\n"
        f"Session cleaned up."
    )


# ---------------------------------------------------------------------------
# File write helpers (mirrors knowledge.py internals to avoid circular import)


def _parse_sections(content: str) -> dict[str, str]:
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
    parts: list[str] = []
    for title, body in sections.items():
        if title == "_preamble":
            if body.strip():
                parts.append(body)
        else:
            parts.append(f"## {title}\n{body}")
    return "\n".join(parts)


def _git_commit_file(knowledge_dir: Path, filepath: Path, message: str) -> None:
    try:
        add = subprocess.run(
            ["git", "add", str(filepath)],
            cwd=knowledge_dir,
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            logger.warning("git add failed for %s: %s", filepath, add.stderr.strip())
            return
        commit = subprocess.run(
            ["git", "commit", "-m", message, "--", str(filepath)],
            cwd=knowledge_dir,
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            logger.warning(
                "git commit failed for %s: %s", filepath, commit.stderr.strip()
            )
    except FileNotFoundError:
        pass  # git not available in dev stdio mode


def _write_section(
    knowledge_dir: Path, scope: str, project: str, section: str, new_content: str
) -> str:
    """Write a section to a knowledge file with file-lock and git commit.

    Returns a status string. knowledge_dir should already be the effective dir.
    """
    safe_scope = _SAFE_NAME_RE.sub("", scope)
    safe_project = _SAFE_NAME_RE.sub("", project)
    filepath = knowledge_dir / safe_scope / f"{safe_project}.md"

    # Symlink traversal guard: resolve and verify path stays within knowledge_dir
    resolved = filepath.resolve()
    if not resolved.is_relative_to(knowledge_dir.resolve()):
        return f"Error: Path escapes knowledge directory: {scope}/{project}"

    filepath.parent.mkdir(parents=True, exist_ok=True)
    lock_path = filepath.with_suffix(".lock")
    lock_path.touch(exist_ok=True)

    with open(lock_path, "r") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if filepath.exists():
                existing = filepath.read_text(encoding="utf-8")
                sections = _parse_sections(existing)
            else:
                sections = {"_preamble": f"# {safe_project}\n\n"}

            if not new_content.endswith("\n"):
                new_content += "\n"
            sections[section] = new_content
            filepath.write_text(_rebuild_markdown(sections), encoding="utf-8")
            _git_commit_file(
                knowledge_dir,
                filepath,
                f"maintain: update {safe_scope}/{safe_project} § {section}",
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)

    return f"Updated {safe_scope}/{safe_project} § {section}"


# ---------------------------------------------------------------------------
# Main registration


def register_maintain_tools(mcp: FastMCP, knowledge_dir: Path) -> None:
    """Register knowledge_maintain and interactive maintain session tools."""

    # ------------------------------------------------------------------
    # Original read-only audit tool (unchanged, backwards compatible)
    # ------------------------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
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

        For an interactive guided hygiene session with targeted questions and
        automatic updates, use maintain_start / maintain_answer /
        maintain_confirm / maintain_skip instead.

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
            lines.append("  ✅ No issues found.")
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
                f"with `meta_update` if needed. "
                f"Or start an interactive session with `maintain_start`."
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Interactive session tools
    # ------------------------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def maintain_start(
        scope: str | None = None,
        stale_days: int = 90,
        generate_questions: bool = True,
    ) -> str:
        """Start an interactive knowledge hygiene session.

        Scans for stale files, extracts content hints (dates, versions, TODOs),
        and generates targeted questions. Returns the session ID and first
        question. Answer questions with maintain_answer, confirm proposed
        changes with maintain_confirm, or skip with maintain_skip.

        Hard cap: 10 questions per session. A new call replaces any
        existing session.

        Args:
            scope: Optional scope filter — e.g. 'work'. If omitted, all readable
                   scopes are included.
            stale_days: Threshold for flagging files as stale. Default 90.
            generate_questions: If False, skip LLM question generation and use
                                 simple hint-based questions (cheap mode).
        """
        meter_call("maintain_start")
        try:
            require("meta:write")
        except PermissionDenied as e:
            return str(e)

        effective_dir = get_effective_knowledge_dir(knowledge_dir)
        allowed = allowed_subscopes("knowledge:read")

        if allowed is not ALL and scope and scope not in allowed:
            return f"Permission denied: no read access to scope '{scope}'."

        # Determine scope directories to scan
        if scope:
            safe_scope = _SAFE_NAME_RE.sub("", scope)
            if not safe_scope:
                return f"Invalid scope {scope!r}."
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
        stale_files_data: list[dict] = []

        for scope_dir in search_dirs:
            if not scope_dir.is_dir():
                continue
            for md_file in sorted(scope_dir.glob("*.md")):
                try:
                    result = subprocess.run(
                        ["git", "log", "-1", "--format=%aI", "--", str(md_file)],
                        cwd=effective_dir,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    date_str = result.stdout.strip()
                    if not date_str:
                        continue
                    last_modified = datetime.fromisoformat(date_str)
                    days_ago = (now - last_modified).days
                    if days_ago < stale_days:
                        continue
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue

                try:
                    content = md_file.read_text(encoding="utf-8")
                except OSError:
                    continue

                hints = extract_content_hints(content)
                stale_files_data.append(
                    {
                        "path": f"{scope_dir.name}/{md_file.stem}",
                        "days_stale": days_ago,
                        "hints": hints,
                        "content": content,
                    }
                )

        if not stale_files_data:
            return f"No files older than {stale_days} days found. Vault looks fresh!"

        # Sort worst-first, cap at MAX_QUESTIONS files for question generation
        stale_files_data.sort(key=lambda x: -x["days_stale"])
        files_for_questions = stale_files_data[:MAX_QUESTIONS]

        if generate_questions:
            questions_raw = _generate_questions_llm(files_for_questions)
        else:
            questions_raw = _generate_questions_cheap(files_for_questions)

        # Build question list with tracking state
        questions = [
            {
                "id": i + 1,
                "file": q.get("file", files_for_questions[i]["path"] if i < len(files_for_questions) else ""),
                "question": q.get("question", ""),
                "hint_context": q.get("hint_context", []),
                "status": "pending",
                "proposed_change": None,
            }
            for i, q in enumerate(questions_raw)
        ]

        session: dict = {
            "session_id": str(uuid.uuid4()),
            "scope": scope,
            "stale_days": stale_days,
            "stale_files": [
                {"path": f["path"], "days_stale": f["days_stale"], "hints": f["hints"]}
                for f in stale_files_data
            ],
            "questions": questions,
            "current_question_index": 0,
            "created_at": now.isoformat(),
        }

        session_path = _get_session_path(effective_dir)
        _save_session(session_path, session)

        total = len(questions)
        header = (
            f"Started maintain session for {len(stale_files_data)} stale file(s). "
            f"{total} question(s) queued.\n\n"
        )
        return header + _format_next_question(session)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def maintain_answer(session_id: str, answer: str) -> str:
        """Answer the current question in a maintain session.

        The server calls an LLM to draft a proposed change based on the answer.
        Returns the proposed change as a before/after diff for review.
        Call maintain_confirm to apply it or maintain_skip to move on.

        If the answer implies no change is needed, the question is automatically
        advanced to the next one.

        Args:
            session_id: The session ID returned by maintain_start.
            answer: Your answer to the current question.
        """
        meter_call("maintain_answer")
        try:
            require("meta:write")
        except PermissionDenied as e:
            return str(e)

        effective_dir = get_effective_knowledge_dir(knowledge_dir)
        session_path = _get_session_path(effective_dir)
        session = _load_session(session_path)

        if session is None:
            return "No active maintain session. Call maintain_start first."
        if session["session_id"] != session_id:
            return f"Session ID mismatch. Active session: {session['session_id']}"

        idx = session["current_question_index"]
        questions = session["questions"]

        if idx >= len(questions):
            _delete_session(session_path)
            return _format_done_summary(session)

        q = questions[idx]

        # Read the current file content
        file_key = q["file"]  # "scope/project"
        parts = file_key.split("/", 1)
        if len(parts) != 2:
            return f"Invalid file reference in session: {file_key!r}"
        file_scope, file_project = parts
        safe_scope = _SAFE_NAME_RE.sub("", file_scope)
        safe_project = _SAFE_NAME_RE.sub("", file_project)
        filepath = effective_dir / safe_scope / f"{safe_project}.md"

        # Defence-in-depth: verify resolved path stays within knowledge dir
        try:
            require_path_within(filepath, effective_dir)
        except PermissionDenied as e:
            return f"Error: {e}"

        if not filepath.exists():
            # File was deleted — skip this question
            questions[idx]["status"] = "skipped"
            session["current_question_index"] = idx + 1
            _save_session(session_path, session)
            return (
                f"File `{file_key}` not found — skipping.\n\n"
                + _format_next_question(session)
            )

        try:
            current_content = filepath.read_text(encoding="utf-8")
        except OSError as e:
            return f"Cannot read {file_key}: {e}"

        # Ask LLM to draft the proposed change
        try:
            import anthropic
            client = anthropic.Anthropic()

            hint_context = q.get("hint_context", [])
            hints_text = ""
            if hint_context:
                if isinstance(hint_context, list):
                    hints_text = "\nContent hints: " + ", ".join(
                        f"{h['type']}={h['value']}" for h in hint_context[:5]
                        if isinstance(h, dict)
                    )
                else:
                    hints_text = f"\nContent hints: {hint_context}"

            system_prompt = (
                "You are a knowledge-base editor. Given a markdown knowledge file and "
                "a user's answer about its currency, propose the minimal edit needed. "
                "Return JSON: "
                '{"needs_change": bool, "section": "section title or null", '
                '"old_text": "exact text to replace or null", '
                '"new_text": "replacement text or null", '
                '"reason": "one-line explanation"}. '
                "Use null for section/old_text/new_text when needs_change is false. "
                "Return ONLY the JSON object."
            )

            user_msg = (
                f"File: {file_key}{hints_text}\n"
                f"Question: {q['question']}\n"
                f"User answer: {answer}\n\n"
                f"Current file content:\n{current_content[:3000]}"
            )

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = response.content[0].text.strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            proposal_raw = json.loads(text)

        except ImportError:
            # No anthropic SDK — treat the answer as the new section content
            proposal_raw = {
                "needs_change": True,
                "section": None,
                "old_text": None,
                "new_text": answer,
                "reason": "Manual update (LLM not available)",
            }
        except Exception as e:
            logger.warning("LLM draft failed: %s", e)
            return (
                f"LLM draft failed: {e}\n\n"
                "Please call maintain_skip to skip this question or retry."
            )

        if not proposal_raw.get("needs_change"):
            # No change needed — auto-advance
            questions[idx]["status"] = "skipped"
            session["current_question_index"] = idx + 1
            _save_session(session_path, session)
            return (
                f"No change needed for `{file_key}` "
                f"({proposal_raw.get('reason', 'answer indicates current')}).\n\n"
                + _format_next_question(session)
            )

        # Build a proposed change diff
        old_text = proposal_raw.get("old_text") or ""
        new_text = proposal_raw.get("new_text") or ""
        section = proposal_raw.get("section")

        # Produce a unified diff for display
        if old_text and new_text:
            diff_lines = list(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"{file_key} (before)",
                    tofile=f"{file_key} (after)",
                    lineterm="",
                )
            )
            diff_text = "".join(diff_lines) or "(no textual diff — content unchanged)"
        else:
            diff_text = f"New content:\n{new_text}"

        # Store proposed change in session
        questions[idx]["proposed_change"] = {
            "scope": safe_scope,
            "project": safe_project,
            "section": section,
            "old_text": old_text,
            "new_text": new_text,
            "full_new_content": None,  # section-level edit
        }
        _save_session(session_path, session)

        reason = proposal_raw.get("reason", "")
        return json.dumps({
            "session_id": session_id,
            "has_change": True,
            "before": old_text,
            "after": new_text,
            "section": section,
            "file": file_key,
            "reason": reason,
            "diff": diff_text,
        })

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def maintain_confirm(session_id: str) -> str:
        """Apply the proposed change from maintain_answer and advance to next question.

        Writes the updated content to the knowledge file and commits it to git.
        Must be called after maintain_answer has drafted a proposed change.

        Args:
            session_id: The session ID returned by maintain_start.
        """
        meter_call("maintain_confirm")
        try:
            require("meta:write")
        except PermissionDenied as e:
            return str(e)

        effective_dir = get_effective_knowledge_dir(knowledge_dir)
        session_path = _get_session_path(effective_dir)
        session = _load_session(session_path)

        if session is None:
            return "No active maintain session. Call maintain_start first."
        if session["session_id"] != session_id:
            return f"Session ID mismatch. Active session: {session['session_id']}"

        idx = session["current_question_index"]
        questions = session["questions"]

        if idx >= len(questions):
            _delete_session(session_path)
            return _format_done_summary(session)

        q = questions[idx]
        proposed = q.get("proposed_change")

        if proposed is None:
            return (
                "No proposed change to confirm. "
                "Call maintain_answer first to draft a change."
            )

        scope = proposed["scope"]
        project = proposed["project"]
        section = proposed.get("section")
        old_text = proposed.get("old_text") or ""
        new_text = proposed.get("new_text") or ""

        # Apply the change
        try:
            filepath = effective_dir / scope / f"{project}.md"
            # Symlink traversal guard: resolve and verify path stays within knowledge dir
            resolved = filepath.resolve()
            if not resolved.is_relative_to(effective_dir.resolve()):
                return f"Error: Path escapes knowledge directory: {scope}/{project}"
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
            else:
                content = f"# {project}\n\n"

            if section:
                # Section-level update
                _write_section(effective_dir, scope, project, section, new_text)
                result_msg = f"Updated `{scope}/{project}` § `{section}`"
            elif old_text and old_text in content:
                # In-place text replacement
                new_content = content.replace(old_text, new_text, 1)
                filepath.parent.mkdir(parents=True, exist_ok=True)
                lock_path = filepath.with_suffix(".lock")
                lock_path.touch(exist_ok=True)
                with open(lock_path, "r") as lock_fd:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    try:
                        filepath.write_text(new_content, encoding="utf-8")
                        _git_commit_file(
                            effective_dir,
                            filepath,
                            f"maintain: update {scope}/{project}",
                        )
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                result_msg = f"Updated `{scope}/{project}`"
            else:
                # Fallback: append new_text as a new section if we can't locate old_text
                _write_section(effective_dir, scope, project, "Updated", new_text)
                result_msg = f"Appended update to `{scope}/{project}` § Updated"

        except Exception as e:
            logger.error("maintain_confirm write failed: %s", e)
            return f"Write failed: {e}. Session still active — retry or call maintain_skip."

        # Advance session
        questions[idx]["status"] = "confirmed"
        session["current_question_index"] = idx + 1

        if session["current_question_index"] >= len(questions):
            _delete_session(session_path)
            # Rebuild summary with updated question list
            return f"✅ {result_msg}\n\n" + _format_done_summary(session)

        _save_session(session_path, session)
        return f"✅ {result_msg}\n\n" + _format_next_question(session)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def maintain_skip(session_id: str) -> str:
        """Skip the current question without making any changes.

        Advances to the next question. Useful when the information is current
        or when you want to defer the decision.

        Args:
            session_id: The session ID returned by maintain_start.
        """
        meter_call("maintain_skip")
        try:
            require("meta:write")
        except PermissionDenied as e:
            return str(e)

        effective_dir = get_effective_knowledge_dir(knowledge_dir)
        session_path = _get_session_path(effective_dir)
        session = _load_session(session_path)

        if session is None:
            return "No active maintain session. Call maintain_start first."
        if session["session_id"] != session_id:
            return f"Session ID mismatch. Active session: {session['session_id']}"

        idx = session["current_question_index"]
        questions = session["questions"]

        if idx >= len(questions):
            _delete_session(session_path)
            return _format_done_summary(session)

        questions[idx]["status"] = "skipped"
        session["current_question_index"] = idx + 1

        if session["current_question_index"] >= len(questions):
            _delete_session(session_path)
            return _format_done_summary(session)

        _save_session(session_path, session)
        return _format_next_question(session)
