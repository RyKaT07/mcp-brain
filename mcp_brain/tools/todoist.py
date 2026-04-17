"""Todoist integration — task management via Todoist REST API v2.

Registers 5 tools: todoist_projects, todoist_sections, todoist_list,
todoist_add, todoist_complete. Requires a Todoist API token passed
from server.py (env var TODOIST_API_KEY). If the token is empty, tools
are not registered and the server starts normally without task management.
"""

from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.rate_limit import RateLimiter
from mcp_brain.tools._perms import require

logger = logging.getLogger(__name__)

_rl_add = RateLimiter("todoist_add", 10.0)
_rl_complete = RateLimiter("todoist_complete", 10.0)

_BASE = "https://api.todoist.com/api/v1"

_PRIORITY_TO_API = {"normal": 1, "medium": 2, "high": 3, "urgent": 4}
_PRIORITY_EMOJI = {4: "\U0001f534", 3: "\U0001f7e0", 2: "\U0001f535", 1: "\u26aa"}
_PRIORITY_LABEL = {4: "urgent", 3: "high", 2: "medium", 1: "normal"}


# -- Module-level HTTP helpers (take api_key as parameter) ------------------

def _todoist_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _todoist_get(api_key: str, path: str, params: dict[str, str] | None = None) -> list | dict:
    url = f"{_BASE}{path}"
    if params:
        filtered = {k: v for k, v in params.items() if v}
        if filtered:
            url = f"{url}?{urlencode(filtered)}"
    req = Request(url, headers=_todoist_headers(api_key), method="GET")
    with urlopen(req, timeout=15) as resp:  # nosemgrep
        result = json.loads(resp.read())
    # API v1 wraps list endpoints in {"results": [...]}
    if isinstance(result, dict) and "results" in result:
        return result["results"]
    return result


def _todoist_post(api_key: str, path: str, body: dict | None = None) -> dict | None:
    url = f"{_BASE}{path}"
    data = json.dumps(body).encode() if body else b""
    req = Request(url, data=data, headers=_todoist_headers(api_key), method="POST")
    with urlopen(req, timeout=15) as resp:  # nosemgrep
        raw = resp.read()
        return json.loads(raw) if raw else None


def fetch_tasks_for_index(api_key: str) -> list[dict]:
    """Fetch all open Todoist tasks with project/section names resolved.

    Returns a list of dicts suitable for SearchIndex.index_todoist_tasks():
      {"content": str, "project_name": str, "section_name": str}

    Returns an empty list on any API error (graceful degradation).
    """
    try:
        projects = _todoist_get(api_key, "/projects")
        project_names: dict[str, str] = {p["id"]: p["name"] for p in projects}

        tasks = _todoist_get(api_key, "/tasks")
        if not tasks:
            return []

        # Resolve section names for all projects that have tasks
        project_ids = {t.get("project_id") for t in tasks if t.get("project_id")}
        section_names: dict[str, str] = {}
        for pid in project_ids:
            sections = _todoist_get(api_key, "/sections", {"project_id": pid})
            for s in sections:
                section_names[s["id"]] = s["name"]

        result = []
        for t in tasks:
            result.append({
                "content": t.get("content", ""),
                "project_name": project_names.get(t.get("project_id", ""), "Inbox"),
                "section_name": section_names.get(t.get("section_id", ""), "") or "",
            })
        return result
    except Exception as exc:
        logger.debug("fetch_tasks_for_index: skipping Todoist indexing (%s)", exc)
        return []


def register_todoist_tools(mcp: FastMCP, api_key: str) -> None:
    """Register Todoist tools on the MCP server.

    Args:
        mcp: FastMCP instance to register tools on.
        api_key: Todoist API token (from Settings -> Integrations -> Developer).
    """

    # -- HTTP helpers (closure delegates to module-level helpers) -----------

    def _get(path: str, params: dict[str, str] | None = None) -> list | dict:
        return _todoist_get(api_key, path, params)

    def _post(path: str, body: dict | None = None) -> dict | None:
        return _todoist_post(api_key, path, body)

    def _api_error(e: HTTPError) -> str:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"Todoist API error ({e.code}): {body}" if body else f"Todoist API error ({e.code})"

    # -- Lookup helpers ------------------------------------------------------

    _project_cache: dict[str, list[dict]] | None = None

    def _get_projects() -> list[dict]:
        nonlocal _project_cache
        if _project_cache is None:
            _project_cache = {"data": _get("/projects")}
        return _project_cache["data"]

    def _find_project_id(name: str) -> str | None:
        lower = name.lower()
        for p in _get_projects():
            if p["name"].lower() == lower:
                return p["id"]
        return None

    def _resolve_project(name: str) -> tuple[str | None, str | None]:
        """Resolve project name to ID. Returns (project_id, error_msg)."""
        pid = _find_project_id(name)
        if pid is None:
            available = ", ".join(p["name"] for p in _get_projects())
            return None, f"Project '{name}' not found. Available: {available}"
        return pid, None

    def _find_section_id(project_id: str, name: str) -> str | None:
        sections = _get("/sections", {"project_id": project_id})
        lower = name.lower()
        for s in sections:
            if s["name"].lower() == lower:
                return s["id"]
        return None

    def _format_task(t: dict) -> str:
        prio = t.get("priority", 1)
        emoji = _PRIORITY_EMOJI.get(prio, "\u26aa")
        due = ""
        if t.get("due"):
            due_info = t["due"]
            date_str = due_info.get("datetime") or due_info.get("date", "")
            if date_str:
                due = f" (due: {date_str})"
            if due_info.get("is_recurring"):
                due += " \U0001f501"
        return f"{emoji} [{t['id']}] {t['content']}{due}"

    # -- Tools ---------------------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    def todoist_projects() -> str:
        """List all Todoist projects.

        Returns project names and IDs. Use project names (not IDs) when
        calling other todoist tools.

        Args: (none)
        """
        try:
            require("todoist:read")
        except PermissionDenied as e:
            return str(e)
        try:
            projects = _get("/projects")
            if not projects:
                return "No projects found."
            lines = [f"- {p['name']} (id: {p['id']})" for p in projects]
            return "## Todoist Projects\n\n" + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Todoist connection error: {e}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    def todoist_sections(project: str) -> str:
        """List sections in a Todoist project.

        Sections group tasks within a project. In the brain mapping,
        Todoist sections correspond to brain projects (files) within
        a brain scope.

        Args:
            project: Project name (case-insensitive).
        """
        try:
            require("todoist:read")
        except PermissionDenied as e:
            return str(e)
        try:
            pid, err = _resolve_project(project)
            if err:
                return err
            sections = _get("/sections", {"project_id": pid})
            if not sections:
                return f"No sections in project '{project}'."
            lines = [f"- {s['name']} (id: {s['id']})" for s in sections]
            return f"## Sections in {project}\n\n" + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Todoist connection error: {e}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    def todoist_list(
        project: str | None = None,
        section: str | None = None,
        filter: str | None = None,
    ) -> str:
        """List tasks from Todoist.

        Returns tasks with priority indicators, IDs, and due dates.
        Use task IDs from the output when completing tasks.

        Args:
            project: Optional project name to filter by (case-insensitive).
            section: Optional section name within the project. Requires
                     project to be set.
            filter: Optional Todoist filter string. Supports Todoist's
                    native syntax, e.g. "today", "overdue", "7 days",
                    "p1", "tomorrow", "#ProjectName", "@label".
        """
        try:
            require("todoist:read")
        except PermissionDenied as e:
            return str(e)
        try:
            params: dict[str, str] = {}
            project_name = None

            if project:
                pid, err = _resolve_project(project)
                if err:
                    return err
                params["project_id"] = pid
                project_name = project

                if section:
                    sid = _find_section_id(pid, section)
                    if sid is None:
                        sections = _get("/sections", {"project_id": pid})
                        available = ", ".join(s["name"] for s in sections) or "(none)"
                        return (
                            f"Section '{section}' not found in '{project}'. "
                            f"Available: {available}"
                        )
                    params["section_id"] = sid

            # Todoist API v1 moved filter to a dedicated endpoint:
            # GET /tasks/filter?query=... (was GET /tasks?filter=... in v2)
            if filter:
                filter_params = {"query": filter}
                tasks = _get("/tasks/filter", filter_params)
            else:
                tasks = _get("/tasks", params if params else None)
            if not tasks:
                label = f" in '{project}'" if project else ""
                label += f" / {section}" if section else ""
                label += f" matching '{filter}'" if filter else ""
                return f"No open tasks{label}."

            # Group tasks by section for readability
            if project and not section:
                sections_map: dict[str | None, list[str]] = {}
                section_names: dict[str | None, str] = {None: "(no section)"}

                # Pre-fetch section names
                pid_resolved = params.get("project_id")
                if pid_resolved:
                    for s in _get("/sections", {"project_id": pid_resolved}):
                        section_names[s["id"]] = s["name"]

                for t in tasks:
                    sec_id = t.get("section_id")
                    sections_map.setdefault(sec_id, []).append(_format_task(t))

                parts = []
                for sec_id, task_lines in sections_map.items():
                    sec_name = section_names.get(sec_id, f"Section {sec_id}")
                    parts.append(f"### {sec_name}")
                    parts.extend(task_lines)
                    parts.append("")

                header = f"## Tasks — {project_name}"
                return header + "\n\n" + "\n".join(parts)

            lines = [_format_task(t) for t in tasks]
            header = "## Tasks"
            if project_name:
                header += f" — {project_name}"
            if section:
                header += f" / {section}"
            if filter:
                header += f" (filter: {filter})"
            return header + "\n\n" + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Todoist connection error: {e}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    def todoist_add(
        content: str,
        project: str | None = None,
        section: str | None = None,
        priority: str = "normal",
        due_string: str | None = None,
        due_lang: str = "en",
        description: str | None = None,
    ) -> str:
        """Add a new task to Todoist.

        Args:
            content: Task title (required). Supports markdown.
            project: Project name (case-insensitive). Defaults to Inbox.
            section: Section name within the project (case-insensitive).
                     If the section does not exist, it will be created.
            priority: Priority level: "normal", "medium", "high", or
                      "urgent". Default: "normal".
            due_string: Natural language due date, e.g. "tomorrow",
                        "next Monday", "Jan 23", "za tydzień" (with
                        due_lang="pl"). Parsed by Todoist.
            due_lang: 2-letter language code for due_string parsing.
                      Default: "en". Use "pl" for Polish dates like
                      "jutro", "w poniedziałek", "za tydzień".
            description: Optional longer description/notes (markdown).
        """
        rate_err = _rl_add.check()
        if rate_err:
            return rate_err
        try:
            require("todoist:write")
        except PermissionDenied as e:
            return str(e)
        try:
            body: dict = {"content": content}

            if project:
                pid, err = _resolve_project(project)
                if err:
                    return err
                body["project_id"] = pid

                if section:
                    pid_str = pid
                    sid = _find_section_id(pid_str, section)
                    if sid is None:
                        # Auto-create missing section
                        new_sec = _post("/sections", {
                            "project_id": pid_str,
                            "name": section,
                        })
                        if new_sec:
                            sid = new_sec["id"]
                            logger.info("Created section '%s' in project '%s'", section, project)
                    if sid:
                        body["section_id"] = sid

            api_priority = _PRIORITY_TO_API.get(priority.lower(), 1)
            if api_priority != 1:
                body["priority"] = api_priority

            if due_string:
                body["due_string"] = due_string
                if due_lang != "en":
                    body["due_lang"] = due_lang

            if description:
                body["description"] = description

            task = _post("/tasks", body)
            if not task:
                return "Task created but no response received."

            prio_label = _PRIORITY_LABEL.get(task.get("priority", 1), "normal")
            result = f"Created: {task['content']} (id: {task['id']}, priority: {prio_label})"
            if task.get("due"):
                result += f", due: {task['due'].get('date', '?')}"
            proj_label = project or "Inbox"
            result += f", project: {proj_label}"
            if section:
                result += f" / {section}"
            return result
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Todoist connection error: {e}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
    def todoist_complete(task_id: str) -> str:
        """Complete (close) a task in Todoist.

        Regular tasks are marked complete and moved to history.
        Recurring tasks are scheduled to their next occurrence.

        Args:
            task_id: The task ID to complete. Get IDs from todoist_list().
        """
        rate_err = _rl_complete.check()
        if rate_err:
            return rate_err
        try:
            require("todoist:write")
        except PermissionDenied as e:
            return str(e)
        try:
            _post(f"/tasks/{task_id}/close")
            return f"Task {task_id} completed."
        except HTTPError as e:
            if e.code == 404:
                return f"Task {task_id} not found."
            return _api_error(e)
        except URLError as e:
            return f"Todoist connection error: {e}"
