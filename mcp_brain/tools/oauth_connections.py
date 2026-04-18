"""
OAuth connection management tools.

These tools allow an authorized client to inspect and revoke the active
OAuth connections on this mcp-brain server.  Each "connection" corresponds
to a cloud MCP client (claude.ai, ChatGPT, Google AI Studio, …) that
completed the OAuth authorization_code + PKCE flow and received a
``tok_oauth_*`` bearer.

Scopes required
---------------
connections:read    oauth_connections_list
connections:write   oauth_connections_revoke

Both scopes are also satisfied by the wildcard ``*``.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import require

if TYPE_CHECKING:
    from mcp_brain.oauth import ChainedProvider


def register_oauth_connections_tools(
    mcp: FastMCP,
    provider: "ChainedProvider",
) -> None:
    """Register oauth_connections_* tools on the MCP server."""

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def oauth_connections_list() -> str:
        """List active OAuth connections.

        Returns each cloud client that has an unexpired access token,
        grouped by client_id.  Includes connection name (if set during
        consent), client name, scopes, and token expiry.

        Requires ``connections:read`` scope (or ``*``).
        """
        try:
            require("connections:read")
        except PermissionDenied as exc:
            return f"Permission denied: {exc.required}"

        connections = provider.store.list_connections()
        if not connections:
            return "No active OAuth connections."

        lines: list[str] = [f"{len(connections)} active connection(s):\n"]
        for conn in connections:
            expires_dt = time.strftime(
                "%Y-%m-%d %H:%M UTC", time.gmtime(conn["expires_at"])
            )
            name_display = conn["connection_name"] or conn["client_name"] or "<unnamed>"
            lines.append(
                f"- name:       {name_display}\n"
                f"  client_id:  {conn['client_id']}\n"
                f"  client:     {conn['client_name'] or '—'}\n"
                f"  scopes:     {' '.join(conn['scopes']) or '*'}\n"
                f"  expires:    {expires_dt}"
            )
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def oauth_connections_revoke(client_id: str) -> str:
        """Revoke all tokens for an OAuth connection.

        Deletes every access token and refresh token associated with
        ``client_id``.  The client will be forced to re-authorize on its
        next request.

        Args:
            client_id: The client_id of the connection to revoke
                       (from ``oauth_connections_list``).

        Requires ``connections:write`` scope (or ``*``).
        """
        try:
            require("connections:write")
        except PermissionDenied as exc:
            return f"Permission denied: {exc.required}"

        access_count = provider.store.delete_access_tokens_for_client(client_id)
        refresh_count = provider.store.delete_refresh_tokens_for_client(client_id)

        if access_count == 0 and refresh_count == 0:
            return f"No active tokens found for client_id={client_id!r}."

        return (
            f"Revoked connection for client_id={client_id!r}: "
            f"{access_count} access token(s) and {refresh_count} refresh token(s) deleted."
        )
