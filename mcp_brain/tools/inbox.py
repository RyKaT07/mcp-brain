"""
Inbox tools — staging area for scraped/proposed knowledge items.

Items land in knowledge/inbox/ as YAML files, await human review,
then get accepted (merged into knowledge) or rejected (archived).
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import require


INBOX_DIR_NAME = "inbox"
ARCHIVE_DIR_NAME = "inbox/_archive"


def _inbox_dir(knowledge_dir: Path) -> Path:
    d = knowledge_dir / INBOX_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archive_dir(knowledge_dir: Path) -> Path:
    d = knowledge_dir / ARCHIVE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def register_inbox_tools(mcp: FastMCP, knowledge_dir: Path):

    @mcp.tool()
    def inbox_list(source: str | None = None, limit: int = 20) -> str:
        """List pending inbox items awaiting review.

        Args:
            source: Optional filter by source (e.g. 'university_portal', 'discord')
            limit: Max items to return (default 20)
        """
        try:
            require("inbox:read")
        except PermissionDenied as e:
            return str(e)

        inbox = _inbox_dir(knowledge_dir)
        items = []

        for f in sorted(inbox.glob("*.yaml"), reverse=True):
            if f.parent.name == "_archive":
                continue
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception:
                continue

            if data.get("status") != "pending":
                continue
            if source and data.get("source") != source:
                continue

            items.append(data)
            if len(items) >= limit:
                break

        if not items:
            return "Inbox is empty."

        lines = []
        for item in items:
            lines.append(
                f"• [{item['id']}] {item.get('summary', 'no summary')} "
                f"(source: {item.get('source', '?')}, {item.get('scraped_at', '?')})"
            )
        return "\n".join(lines)

    @mcp.tool()
    def inbox_show(item_id: str) -> str:
        """Show the full contents of an inbox item, including raw_snippet.

        Use this before inbox_accept when you want the full original
        context rather than just the one-line summary from inbox_list.
        Looks in pending items first, then falls back to the archive so
        you can also inspect previously accepted or rejected items.

        Args:
            item_id: The item ID to display
        """
        try:
            require("inbox:read")
        except PermissionDenied as e:
            return str(e)

        inbox = _inbox_dir(knowledge_dir)
        item_file, item_data = _find_item(inbox, item_id)

        if not item_file:
            # Fall back to archive — accepted / rejected items stay
            # readable so the user can audit past decisions.
            archive = _archive_dir(knowledge_dir)
            for f in archive.glob("*.yaml"):
                try:
                    data = yaml.safe_load(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if data and data.get("id") == item_id:
                    item_file, item_data = f, data
                    break

        if not item_file:
            return f"Item {item_id} not found."

        return yaml.dump(item_data, allow_unicode=True, sort_keys=False)

    @mcp.tool()
    def inbox_add(
        source: str,
        summary: str,
        raw_snippet: str = "",
        suggested_target: str = "",
        suggested_section: str = "",
    ) -> str:
        """Add a new item to the inbox for human review.

        Args:
            source: Where this came from (e.g. 'university_portal', 'discord', 'manual')
            summary: Short description of the information
            raw_snippet: Original text snippet (for reference)
            suggested_target: Suggested knowledge file (e.g. 'school/power-electronics')
            suggested_section: Suggested section within that file
        """
        try:
            require("inbox:write")
        except PermissionDenied as e:
            return str(e)

        inbox = _inbox_dir(knowledge_dir)
        item_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()

        item = {
            "id": item_id,
            "source": source,
            "scraped_at": now,
            "summary": summary,
            "raw_snippet": raw_snippet,
            "suggested_target": suggested_target,
            "suggested_section": suggested_section,
            "status": "pending",
        }

        filename = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}_{item_id}.yaml"
        (inbox / filename).write_text(yaml.dump(item, allow_unicode=True), encoding="utf-8")

        return f"Added to inbox: [{item_id}] {summary}"

    @mcp.tool()
    def inbox_accept(item_id: str) -> str:
        """Accept an inbox item — merge its content into the target knowledge file.

        Args:
            item_id: The item ID to accept
        """
        try:
            require("inbox:write")
        except PermissionDenied as e:
            return str(e)

        inbox = _inbox_dir(knowledge_dir)
        item_file, item_data = _find_item(inbox, item_id)

        if not item_file:
            return f"Item {item_id} not found."

        if not item_data.get("suggested_target"):
            return f"Item {item_id} has no suggested_target. Update it first or merge manually."

        # Import here to avoid circular deps
        from mcp_brain.tools.knowledge import (
            _git_commit,
            _parse_sections,
            _rebuild_markdown,
            _resolve_file,
            _validate_scope_project,
            _validate_scope_writable,
        )

        target_parts = item_data["suggested_target"].split("/", 1)
        if len(target_parts) != 2:
            return f"Invalid target format: {item_data['suggested_target']}. Expected 'scope/project'."

        scope, project = target_parts

        # Accepting writes to a knowledge file → also need write on that scope.
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

        section = item_data.get("suggested_section", "imported")
        filepath = _resolve_file(knowledge_dir, scope, project)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        if filepath.exists():
            sections = _parse_sections(filepath.read_text(encoding="utf-8"))
        else:
            sections = {"_preamble": f"# {project}\n\n"}

        # Append to section rather than overwrite
        existing = sections.get(section, "")
        entry = f"- [{item_data.get('scraped_at', 'unknown')}] {item_data['summary']}\n"
        sections[section] = existing + entry

        filepath.write_text(_rebuild_markdown(sections), encoding="utf-8")
        _git_commit(knowledge_dir, filepath, f"inbox accept {item_id} → {scope}/{project} § {section}")

        # Move to archive
        item_data["status"] = "accepted"
        item_file.write_text(yaml.dump(item_data, allow_unicode=True), encoding="utf-8")
        archive = _archive_dir(knowledge_dir)
        item_file.rename(archive / item_file.name)

        return f"Accepted [{item_id}] → {scope}/{project} § {section}"

    @mcp.tool()
    def inbox_reject(item_id: str) -> str:
        """Reject an inbox item — archive it without merging.

        Args:
            item_id: The item ID to reject
        """
        try:
            require("inbox:write")
        except PermissionDenied as e:
            return str(e)

        inbox = _inbox_dir(knowledge_dir)
        item_file, item_data = _find_item(inbox, item_id)

        if not item_file:
            return f"Item {item_id} not found."

        item_data["status"] = "rejected"
        item_file.write_text(yaml.dump(item_data, allow_unicode=True), encoding="utf-8")
        archive = _archive_dir(knowledge_dir)
        item_file.rename(archive / item_file.name)

        return f"Rejected [{item_id}]"


def _find_item(inbox: Path, item_id: str) -> tuple[Path | None, dict | None]:
    for f in inbox.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data.get("id") == item_id:
                return f, data
        except Exception:
            continue
    return None, None
