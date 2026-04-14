"""
Brain wake tool — dedicated entry point for wake-word activation.

When an LLM sees a user message containing the configured wake word
(default: "jarvis"), this tool should be called FIRST to load full
context: user briefing, active policies, and behavioral rules.

The wake word is read from `meta.yaml` at server startup and baked
into the tool description. Changing it requires a container restart
(tool descriptions are static in FastMCP — set once at registration).
"""

from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from mcp_brain.tools._perms import ALL, allowed_subscopes


def _load_wake_word(knowledge_dir: Path) -> str:
    """Read ``preferences.wake_word`` from meta.yaml, default ``jarvis``."""
    meta_path = knowledge_dir / "meta.yaml"
    if not meta_path.exists():
        return "jarvis"
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        return (meta or {}).get("preferences", {}).get("wake_word", "jarvis")
    except Exception:
        return "jarvis"


def _load_policy_summary(knowledge_dir: Path) -> str:
    """Load ``_meta/write-policy.md`` in full (returned inside brain_wake output)."""
    policy_path = knowledge_dir / "_meta" / "write-policy.md"
    if not policy_path.exists():
        return ""
    try:
        return policy_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def register_wake_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    *,
    briefing_trigger: str = "",
) -> None:
    """Register the ``brain_wake`` tool on *mcp*.

    Must be called **after** all other ``register_*_tools`` so the tool
    inventory snapshot (captured at registration time) is complete.
    """
    wake_word = _load_wake_word(knowledge_dir)

    description = (
        f"Call this FIRST whenever the user says '{wake_word}' or asks "
        f"about their notes, tasks, calendar, or memory. Returns full "
        f"context: user briefing, active policies, and available tools.\n"
        f"\n"
        f"This is the primary entry point for brain activation. Rules for "
        f"all MCP clients (Claude, ChatGPT, Gemini, and others):\n"
        f"1. When the wake word '{wake_word}' appears anywhere in the user's "
        f"message, call this tool before doing anything else.\n"
        f"2. When the user asks what tools or memory you have access to, "
        f"call this tool first.\n"
        f"3. When the user references personal notes, knowledge files, tasks, "
        f"calendar events, or any stored information, call this tool first.\n"
        f"4. Do NOT call other brain tools before calling brain_wake — "
        f"this tool loads the configuration and policies that govern all "
        f"other tools."
    )
    if briefing_trigger:
        description = f"{briefing_trigger.strip()}\n\n---\n\n{description}"

    # Snapshot registered tools at import time (all other tools are
    # already registered because wake tools are registered last).
    tool_lines: list[str] = []
    try:
        for t in sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name):
            if t.name == "brain_wake":
                continue
            first_line = (t.description or "").strip().split("\n")[0][:100]
            tool_lines.append(f"- **{t.name}**: {first_line}")
    except Exception:
        pass

    @mcp.tool(description=description)
    def brain_wake() -> str:
        """Activate brain context. Returns briefing, policy, and tool inventory."""
        meta_path = knowledge_dir / "meta.yaml"
        warnings: list[str] = []

        # ── Load and validate meta.yaml ────────────────────────────
        if not meta_path.exists():
            return (
                "No meta.yaml found. Create one in the knowledge directory.\n"
                "See meta.yaml.example for the required structure."
            )

        try:
            raw = meta_path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Could not read meta.yaml: {exc}"

        try:
            meta = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return f"meta.yaml contains invalid YAML: {exc}"

        if not isinstance(meta, dict):
            return (
                f"meta.yaml must be a YAML mapping at the top level, "
                f"got {type(meta).__name__}. "
                "Check the file structure against meta.yaml.example."
            )

        parts: list[str] = []

        # ── User identity ──────────────────────────────────────────
        user = meta.get("user")
        if not isinstance(user, dict):
            warnings.append(
                "'user' section missing or not a mapping — add user.name and user.timezone"
            )
            user = {}
        else:
            if "name" not in user:
                warnings.append("'user.name' not set in meta.yaml")
            if "timezone" not in user:
                warnings.append("'user.timezone' not set in meta.yaml")

        parts.append(f"# Brain activated — {user.get('name', 'User')}")
        parts.append(f"Timezone: {user.get('timezone', 'unknown')}")
        parts.append("")

        # ── Preferences ────────────────────────────────────────────
        prefs = meta.get("preferences")
        if prefs is not None and not isinstance(prefs, dict):
            warnings.append(
                f"'preferences' must be a mapping, got {type(prefs).__name__} — skipped"
            )
            prefs = {}
        if prefs:
            parts.append("## Preferences")
            for k, v in prefs.items():
                parts.append(f"- {k}: {v}")
            parts.append("")

        # ── Projects (filtered by token permissions) ───────────────
        projects = meta.get("projects")
        if projects is not None and not isinstance(projects, dict):
            warnings.append(
                f"'projects' must be a mapping, got {type(projects).__name__} — skipped"
            )
            projects = {}
        elif projects is None:
            projects = {}

        all_scopes = list(projects.keys())
        try:
            allowed = allowed_subscopes("briefing")
        except Exception:
            allowed = ALL
        scopes = [s for s in all_scopes if allowed is ALL or s in allowed]

        for s in scopes:
            proj_meta = projects.get(s)
            if proj_meta is not None and not isinstance(proj_meta, dict):
                warnings.append(
                    f"projects.{s} must be a mapping, got {type(proj_meta).__name__} — skipped"
                )
                proj_meta = {}
            elif proj_meta is None:
                proj_meta = {}

            if proj_meta:
                parts.append(f"## {s}")
                for k, v in proj_meta.items():
                    parts.append(f"- {k}: {v}")
                parts.append("")

            scope_dir = knowledge_dir / s
            if scope_dir.exists():
                try:
                    files = sorted(scope_dir.glob("*.md"))
                    if files:
                        parts.append(f"### Available knowledge files ({s}/)")
                        for f in files:
                            parts.append(f"- {f.stem}")
                        parts.append("")
                except OSError as exc:
                    warnings.append(f"Could not list {s}/ knowledge files: {exc}")

        # ── Active policy rules ────────────────────────────────────
        policy = _load_policy_summary(knowledge_dir)
        if policy:
            parts.append("## Active policy rules")
            parts.append("")
            parts.append(policy)
            parts.append("")

        # ── Available tools ────────────────────────────────────────
        if tool_lines:
            parts.append("## Available tools")
            parts.extend(tool_lines)
            parts.append("")

        # ── Configuration warnings (shown last so they don't bury context)
        if warnings:
            parts.append("## Configuration warnings")
            for w in warnings:
                parts.append(f"- {w}")
            parts.append("")

        return "\n".join(parts)
