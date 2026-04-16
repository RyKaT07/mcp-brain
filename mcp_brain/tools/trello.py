"""Trello integration — kanban board access via Trello REST API.

Registers 5 tools: trello_boards, trello_lists, trello_cards,
trello_add_card, trello_move_card. Requires TRELLO_API_KEY and
TRELLO_API_TOKEN env vars. If either is empty, tools are not
registered and the server starts normally without Trello access.
"""

from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.rate_limit import RateLimiter
from mcp_brain.tools._perms import require

logger = logging.getLogger(__name__)

_rl_add_card = RateLimiter("trello_add_card", 10.0)
_rl_move_card = RateLimiter("trello_move_card", 10.0)

_BASE = "https://api.trello.com/1"


def register_trello_tools(mcp: FastMCP, api_key: str, api_token: str) -> None:
    """Register Trello tools on the MCP server.

    Args:
        mcp: FastMCP instance to register tools on.
        api_key: Trello API key (from https://trello.com/power-ups/admin).
        api_token: Trello API token (generated during Power-Up setup).
    """

    # -- HTTP helpers (auth via query params, not bearer) --------------------

    def _auth_params() -> dict[str, str]:
        return {"key": api_key, "token": api_token}

    def _get(path: str, extra_params: dict[str, str] | None = None) -> list | dict:
        params = {**_auth_params(), **(extra_params or {})}
        url = f"{_BASE}{path}?{urlencode(params)}"
        req = Request(url, method="GET")
        with urlopen(req, timeout=15) as resp:  # nosemgrep
            return json.loads(resp.read())

    def _post(path: str, extra_params: dict[str, str] | None = None) -> dict:
        params = {**_auth_params(), **(extra_params or {})}
        url = f"{_BASE}{path}?{urlencode(params)}"
        req = Request(url, data=b"", method="POST")
        with urlopen(req, timeout=15) as resp:  # nosemgrep
            return json.loads(resp.read())

    def _put(path: str, extra_params: dict[str, str] | None = None) -> dict:
        params = {**_auth_params(), **(extra_params or {})}
        url = f"{_BASE}{path}?{urlencode(params)}"
        req = Request(url, data=b"", method="PUT")
        with urlopen(req, timeout=15) as resp:  # nosemgrep
            return json.loads(resp.read())

    def _api_error(e: HTTPError) -> str:
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            body = ""
        return f"Trello API error ({e.code}): {body}" if body else f"Trello API error ({e.code})"

    # -- Lookup helpers ------------------------------------------------------

    _board_cache: dict[str, list[dict]] | None = None

    def _get_boards() -> list[dict]:
        nonlocal _board_cache
        if _board_cache is None:
            boards = _get("/members/me/boards", {"fields": "name,url,closed"})
            _board_cache = {"data": [b for b in boards if not b.get("closed")]}
        return _board_cache["data"]

    def _find_board_id(name: str) -> str | None:
        lower = name.lower()
        for b in _get_boards():
            if b["name"].lower() == lower:
                return b["id"]
        return None

    def _resolve_board(name: str) -> tuple[str | None, str | None]:
        """Resolve board name to ID. Returns (board_id, error_msg)."""
        bid = _find_board_id(name)
        if bid is None:
            available = ", ".join(b["name"] for b in _get_boards())
            return None, f"Board '{name}' not found. Available: {available}"
        return bid, None

    def _find_list_id(board_id: str, name: str) -> str | None:
        lists = _get(f"/boards/{board_id}/lists", {"fields": "name,closed"})
        lower = name.lower()
        for lst in lists:
            if not lst.get("closed") and lst["name"].lower() == lower:
                return lst["id"]
        return None

    def _resolve_list(board_id: str, board_name: str, list_name: str) -> tuple[str | None, str | None]:
        """Resolve list name to ID. Returns (list_id, error_msg)."""
        lid = _find_list_id(board_id, list_name)
        if lid is None:
            lists = _get(f"/boards/{board_id}/lists", {"fields": "name,closed"})
            available = ", ".join(l["name"] for l in lists if not l.get("closed"))
            return None, f"List '{list_name}' not found on board '{board_name}'. Available: {available}"
        return lid, None

    def _format_card(card: dict) -> str:
        parts = [f"[{card['id'][:8]}] {card['name']}"]
        if card.get("due"):
            parts.append(f"(due: {card['due'][:10]})")
        if card.get("labels"):
            label_names = [l.get("name") or l.get("color", "?") for l in card["labels"]]
            parts.append(f"[{', '.join(label_names)}]")
        return " ".join(parts)

    # -- Tools ---------------------------------------------------------------

    @mcp.tool()
    def trello_boards() -> str:
        """List all open Trello boards.

        Returns board names, IDs, and URLs. Use board names (not IDs)
        when calling other trello tools.

        Args: (none)
        """
        try:
            require("trello:read")
        except PermissionDenied as e:
            return str(e)
        try:
            boards = _get_boards()
            if not boards:
                return "No open boards found."
            lines = [f"- {b['name']} (id: {b['id'][:8]})" for b in boards]
            return "## Trello Boards\n\n" + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Trello connection error: {e}"

    @mcp.tool()
    def trello_lists(board: str) -> str:
        """List all open lists on a Trello board.

        Lists represent columns on the kanban board (e.g. Backlog,
        In Progress, Review, Done).

        Args:
            board: Board name (case-insensitive).
        """
        try:
            require("trello:read")
        except PermissionDenied as e:
            return str(e)
        try:
            bid, err = _resolve_board(board)
            if err:
                return err
            lists = _get(f"/boards/{bid}/lists", {"fields": "name,closed"})
            open_lists = [l for l in lists if not l.get("closed")]
            if not open_lists:
                return f"No open lists on board '{board}'."
            lines = [f"- {l['name']} (id: {l['id'][:8]})" for l in open_lists]
            return f"## Lists on {board}\n\n" + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Trello connection error: {e}"

    @mcp.tool()
    def trello_cards(board: str, list: str | None = None) -> str:
        """List cards on a Trello board, optionally filtered by list.

        Returns card names, IDs, due dates, and labels. When no list
        is specified, cards are grouped by list for a kanban overview.

        Args:
            board: Board name (case-insensitive).
            list: Optional list name to filter by (case-insensitive).
                  If omitted, shows all cards grouped by list.
        """
        try:
            require("trello:read")
        except PermissionDenied as e:
            return str(e)
        try:
            bid, err = _resolve_board(board)
            if err:
                return err

            if list:
                lid, err = _resolve_list(bid, board, list)
                if err:
                    return err
                cards = _get(f"/lists/{lid}/cards", {
                    "fields": "name,url,due,labels,idList",
                })
                if not cards:
                    return f"No cards in '{list}' on board '{board}'."
                lines = [_format_card(c) for c in cards]
                return f"## {board} / {list}\n\n" + "\n".join(lines)

            # All cards grouped by list
            cards = _get(f"/boards/{bid}/cards", {
                "fields": "name,url,due,labels,idList",
            })
            if not cards:
                return f"No cards on board '{board}'."

            # Fetch list names for grouping
            lists_data = _get(f"/boards/{bid}/lists", {"fields": "name,closed"})
            list_names = {l["id"]: l["name"] for l in lists_data if not l.get("closed")}

            # Group cards by list
            by_list: dict[str, list[str]] = {}
            for c in cards:
                list_name = list_names.get(c.get("idList", ""), "Unknown")
                by_list.setdefault(list_name, []).append(_format_card(c))

            parts = []
            # Preserve list order from board
            ordered_names = [l["name"] for l in lists_data if not l.get("closed")]
            for ln in ordered_names:
                if ln in by_list:
                    parts.append(f"### {ln}")
                    parts.extend(by_list[ln])
                    parts.append("")

            return f"## {board}\n\n" + "\n".join(parts)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Trello connection error: {e}"

    @mcp.tool()
    def trello_add_card(
        name: str,
        board: str,
        list: str,
        description: str | None = None,
        due: str | None = None,
    ) -> str:
        """Add a new card to a Trello board.

        Args:
            name: Card title (required).
            board: Board name (case-insensitive).
            list: List name to add the card to (case-insensitive).
                  E.g. "Backlog", "To Do", "In Progress".
            description: Optional card description (markdown supported).
            due: Optional due date in ISO 8601 format (e.g. "2026-04-15").
        """
        rate_err = _rl_add_card.check()
        if rate_err:
            return rate_err
        try:
            require("trello:write")
        except PermissionDenied as e:
            return str(e)
        try:
            bid, err = _resolve_board(board)
            if err:
                return err
            lid, err = _resolve_list(bid, board, list)
            if err:
                return err

            params: dict[str, str] = {"name": name, "idList": lid}
            if description:
                params["desc"] = description
            if due:
                params["due"] = due

            card = _post("/cards", params)
            result = f"Created: {card['name']} (id: {card['id'][:8]})"
            result += f", board: {board}, list: {list}"
            if card.get("url"):
                result += f"\n{card['url']}"
            return result
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Trello connection error: {e}"

    @mcp.tool()
    def trello_move_card(card_id: str, board: str, list: str) -> str:
        """Move a card to a different list on the same board.

        Use this to update card status on the kanban board
        (e.g. move from "In Progress" to "Done").

        Args:
            card_id: Card ID (get from trello_cards output). Can be
                     the full ID or the short 8-char prefix shown in listings.
            board: Board name (case-insensitive).
            list: Target list name (case-insensitive).
        """
        rate_err = _rl_move_card.check()
        if rate_err:
            return rate_err
        try:
            require("trello:write")
        except PermissionDenied as e:
            return str(e)
        try:
            bid, err = _resolve_board(board)
            if err:
                return err
            lid, err = _resolve_list(bid, board, list)
            if err:
                return err

            card = _put(f"/cards/{card_id}", {"idList": lid})
            return f"Moved '{card['name']}' to list '{list}' on board '{board}'."
        except HTTPError as e:
            if e.code == 404:
                return f"Card '{card_id}' not found."
            return _api_error(e)
        except URLError as e:
            return f"Trello connection error: {e}"
