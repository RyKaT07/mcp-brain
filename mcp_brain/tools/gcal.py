"""Google Calendar integration — read and create events via Calendar API v3.

Registers 3 tools: gcal_calendars, gcal_events, gcal_add_event. Requires
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN env vars.
The refresh token is obtained once via scripts/google-auth.py; mcp-brain
auto-refreshes the access token in memory.

If any env var is missing, tools are not registered and the server starts
normally without calendar access.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import require

logger = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/calendar/v3"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class _TokenManager:
    """Manages Google OAuth 2.0 access tokens via refresh_token.

    On first API call (or when expired), exchanges the long-lived
    refresh_token for a short-lived access_token (~1 hour). The
    refresh_token itself never expires unless revoked.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0

    def get_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        self._refresh()
        if self._access_token is None:
            raise RuntimeError("Failed to obtain Google access token")
        return self._access_token

    def _refresh(self) -> None:
        body = urlencode({
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }).encode()
        req = Request(_TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600)
        logger.info("Google access token refreshed, expires in %ds", data.get("expires_in", 3600))


def register_gcal_tools(
    mcp: FastMCP,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> None:
    """Register Google Calendar tools on the MCP server.

    Args:
        mcp: FastMCP instance to register tools on.
        client_id: Google OAuth 2.0 client ID.
        client_secret: Google OAuth 2.0 client secret.
        refresh_token: Long-lived refresh token from scripts/google-auth.py.
    """

    token_manager = _TokenManager(client_id, client_secret, refresh_token)

    # -- HTTP helpers --------------------------------------------------------

    def _headers() -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token_manager.get_token()}",
            "Content-Type": "application/json",
        }

    def _get(path: str, params: dict[str, str] | None = None) -> dict:
        url = f"{_API_BASE}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        req = Request(url, headers=_headers(), method="GET")
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def _post(path: str, body: dict) -> dict:
        url = f"{_API_BASE}{path}"
        data = json.dumps(body).encode()
        req = Request(url, data=data, headers=_headers(), method="POST")
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def _api_error(e: HTTPError) -> str:
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            body = ""
        return f"Google Calendar error ({e.code}): {body}" if body else f"Google Calendar error ({e.code})"

    # -- Helpers -------------------------------------------------------------

    def _find_calendar_id(name: str) -> str | None:
        """Find calendar ID by name (case-insensitive). Returns None if not found."""
        result = _get("/users/me/calendarList")
        lower = name.lower()
        for cal in result.get("items", []):
            summary = cal.get("summary", "")
            if summary.lower() == lower:
                return cal["id"]
        return None

    def _format_event(event: dict) -> str:
        """Format a calendar event into a readable line."""
        title = event.get("summary", "(no title)")

        start = event.get("start", {})
        end = event.get("end", {})

        # dateTime for timed events, date for all-day events
        start_str = start.get("dateTime", start.get("date", "?"))
        end_str = end.get("dateTime", end.get("date", ""))

        # Simplify datetime display
        if "T" in start_str:
            # "2026-04-11T14:00:00+02:00" → "2026-04-11 14:00"
            start_display = start_str[:16].replace("T", " ")
        else:
            start_display = start_str

        if end_str and "T" in end_str:
            end_display = end_str[11:16]  # just time portion
        elif end_str:
            end_display = ""
        else:
            end_display = ""

        time_range = start_display
        if end_display:
            time_range += f"–{end_display}"

        parts = [f"- {time_range} — {title}"]
        if event.get("location"):
            parts.append(f"  📍 {event['location']}")

        return "\n".join(parts)

    # -- Tools ---------------------------------------------------------------

    @mcp.tool()
    def gcal_calendars() -> str:
        """List available Google Calendars.

        Returns calendar names and IDs. Use calendar names when
        filtering events or creating events in a specific calendar.

        Args: (none)
        """
        try:
            require("gcal:read")
        except PermissionDenied as e:
            return str(e)
        try:
            result = _get("/users/me/calendarList")
            calendars = result.get("items", [])
            if not calendars:
                return "No calendars found."
            lines = []
            for cal in calendars:
                name = cal.get("summary", "(unnamed)")
                primary = " ⭐" if cal.get("primary") else ""
                lines.append(f"- {name}{primary} (id: {cal['id'][:30]}...)")
            return "## Google Calendars\n\n" + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Google Calendar connection error: {e}"

    @mcp.tool()
    def gcal_events(calendar: str | None = None, days: int = 7) -> str:
        """List upcoming events from Google Calendar.

        Returns events sorted by start time with title, time, and location.

        Args:
            calendar: Calendar name (case-insensitive). If omitted, uses
                      the primary calendar.
            days: Number of days ahead to look. Default: 7.
        """
        try:
            require("gcal:read")
        except PermissionDenied as e:
            return str(e)
        try:
            cal_id = "primary"
            cal_label = "primary"
            if calendar:
                found = _find_calendar_id(calendar)
                if found is None:
                    result = _get("/users/me/calendarList")
                    available = ", ".join(
                        c.get("summary", "?") for c in result.get("items", [])
                    )
                    return f"Calendar '{calendar}' not found. Available: {available}"
                cal_id = found
                cal_label = calendar

            now = datetime.now(timezone.utc)
            time_min = now.isoformat()
            time_max = (now + timedelta(days=days)).isoformat()

            result = _get(f"/calendars/{cal_id}/events", {
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "50",
            })

            events = result.get("items", [])
            if not events:
                return f"No events in the next {days} days ({cal_label})."

            lines = [_format_event(e) for e in events]
            return f"## Events — {cal_label} (next {days} days)\n\n" + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Google Calendar connection error: {e}"

    @mcp.tool()
    def gcal_add_event(
        title: str,
        start: str,
        end: str,
        calendar: str | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> str:
        """Create a new event in Google Calendar.

        Args:
            title: Event title (required).
            start: Start datetime in ISO 8601 format with timezone.
                   Example: "2026-04-15T14:00:00+02:00"
                   For all-day events use date only: "2026-04-15"
            end: End datetime in ISO 8601 format with timezone.
                 Example: "2026-04-15T15:00:00+02:00"
                 For all-day events: "2026-04-16" (exclusive end date)
            calendar: Calendar name (case-insensitive). If omitted,
                      uses the primary calendar.
            description: Optional event description.
            location: Optional event location.
        """
        try:
            require("gcal:write")
        except PermissionDenied as e:
            return str(e)
        try:
            cal_id = "primary"
            if calendar:
                found = _find_calendar_id(calendar)
                if found is None:
                    result = _get("/users/me/calendarList")
                    available = ", ".join(
                        c.get("summary", "?") for c in result.get("items", [])
                    )
                    return f"Calendar '{calendar}' not found. Available: {available}"
                cal_id = found

            body: dict = {"summary": title}

            # Detect all-day vs timed event
            if "T" in start:
                body["start"] = {"dateTime": start}
                body["end"] = {"dateTime": end}
            else:
                body["start"] = {"date": start}
                body["end"] = {"date": end}

            if description:
                body["description"] = description
            if location:
                body["location"] = location

            event = _post(f"/calendars/{cal_id}/events", body)
            result_str = f"Created: {event.get('summary', title)}"
            if event.get("start", {}).get("dateTime"):
                result_str += f" ({event['start']['dateTime'][:16].replace('T', ' ')})"
            elif event.get("start", {}).get("date"):
                result_str += f" ({event['start']['date']}, all-day)"
            if event.get("htmlLink"):
                result_str += f"\n{event['htmlLink']}"
            return result_str
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Google Calendar connection error: {e}"
