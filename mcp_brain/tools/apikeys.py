"""
API key management tools — admin-only MCP interface.

These tools allow an authorized client (one holding `apikeys:write` or
`apikeys:read` scope) to manage dynamic API keys for other users.
They are the programmatic counterpart to manually editing `auth.yaml`.

Scopes required
---------------
apikeys:write   apikeys_create, apikeys_revoke
apikeys:read    apikeys_list, apikeys_usage

Usage workflow
--------------
1. Admin calls `apikeys_create(user_id="alice", scopes=["knowledge:read:*"])`.
2. The returned token string is shown ONCE — the admin passes it to Alice.
3. Alice configures her MCP client with that bearer token.
4. Alice's tool calls are namespaced under `knowledge/users/alice/`.
5. Admin can call `apikeys_usage()` to see per-key call counts for billing.
6. Admin calls `apikeys_revoke(key_id="<uuid>")` to disable Alice's access.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.keystore import KeyStore
from mcp_brain.usage import UsageMeter
from mcp_brain.tools._perms import require


def register_apikeys_tools(
    mcp: FastMCP,
    key_store: KeyStore,
    usage_meter: UsageMeter,
) -> None:
    """Register apikeys_* tools on the MCP server."""

    @mcp.tool()
    def apikeys_create(
        user_id: str,
        scopes: list[str],
        description: str | None = None,
    ) -> str:
        """Create a new API key for a user.

        Requires `apikeys:write` scope.

        The returned token is displayed ONCE — save it immediately.
        It is not possible to retrieve the token value later.

        Args:
            user_id: Opaque user identifier — email, handle, or UUID.
                     Knowledge files for this key live under
                     `knowledge/users/{user_id}/`.
            scopes:  Scope list for the new key, e.g.
                     `["knowledge:read:*", "knowledge:write:*"]`.
            description: Optional human-readable label (key name, device, etc.)
        """
        try:
            require("apikeys:write")
        except PermissionDenied as exc:
            return f"Permission denied: {exc.required}"

        entry = key_store.generate(
            user_id=user_id,
            scopes=scopes,
            description=description,
        )
        return (
            f"API key created.\n\n"
            f"  id:          {entry.id}\n"
            f"  user_id:     {entry.user_id}\n"
            f"  description: {entry.description or '—'}\n"
            f"  scopes:      {entry.scopes}\n"
            f"  created_at:  {entry.created_at.isoformat()}\n\n"
            f"  token: {entry.token}\n\n"
            f"Save the token now — it will not be shown again."
        )

    @mcp.tool()
    def apikeys_revoke(key_id: str) -> str:
        """Revoke an API key by its UUID.

        Requires `apikeys:write` scope.
        Revocation is immediate — the key stops working for new requests.

        Args:
            key_id: The UUID of the key to revoke (from `apikeys_list`).
        """
        try:
            require("apikeys:write")
        except PermissionDenied as exc:
            return f"Permission denied: {exc.required}"

        ok = key_store.revoke(key_id)
        if ok:
            return f"Key {key_id} revoked successfully."
        return f"Key {key_id} not found or already revoked."

    @mcp.tool()
    def apikeys_list(user_id: str | None = None) -> str:
        """List active API keys.

        Requires `apikeys:read` scope.
        Token secrets are never included in the output.

        Args:
            user_id: Optional filter — show only keys for this user.
                     Omit to list all active keys.
        """
        try:
            require("apikeys:read")
        except PermissionDenied as exc:
            return f"Permission denied: {exc.required}"

        keys = key_store.list_keys(user_id=user_id)
        if not keys:
            suffix = f" for user '{user_id}'" if user_id else ""
            return f"No active keys{suffix}."

        lines = [
            f"- {k.id}  user={k.user_id}  "
            f"scopes={k.scopes}  "
            f"created={k.created_at.date()}  "
            f"desc={k.description or '—'}"
            for k in keys
        ]
        header = f"{len(lines)} active key(s)"
        if user_id:
            header += f" for user '{user_id}'"
        return header + ":\n" + "\n".join(lines)

    @mcp.tool()
    def apikeys_usage(key_id: str | None = None) -> str:
        """Show per-key usage statistics (call counts per tool).

        Requires `apikeys:read` scope.
        Useful for billing — shows total calls and breakdown by tool name.

        Args:
            key_id: Optional — show stats for a single key UUID.
                    Omit to return stats for all keys.
        """
        try:
            require("apikeys:read")
        except PermissionDenied as exc:
            return f"Permission denied: {exc.required}"

        stats = usage_meter.stats(key_id=key_id)
        if not stats:
            return "No usage data recorded yet."
        return json.dumps(stats, indent=2)
