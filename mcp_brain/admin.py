"""
HTTP admin endpoints for bearer token management.

Protected by MCP_ADMIN_SECRET (X-Admin-Secret header or
Authorization: Bearer <ADMIN_SECRET>).

Routes
------
POST   /admin/tokens                   — create a new bearer token
  body: { label, scopes[], note? }
  response (201): { token_id, token_value, label, scopes, note, created_at }

DELETE /admin/tokens/{token_id}        — revoke a token
  response: { revoked: true, token_id }

GET    /admin/tokens                   — list all tokens (no secret values)
  query: include_revoked=true (optional)
  response: { tokens: [...] }

All endpoints return 401 if the admin secret is wrong or missing.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_brain.keystore import KeyStore


def _check_admin_auth(request: Request, admin_secret: str) -> bool:
    """Return True if the request carries a valid admin secret."""
    if not admin_secret:
        return False
    if request.headers.get("x-admin-secret") == admin_secret:
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == admin_secret:
        return True
    return False


def build_admin_routes(
    key_store: KeyStore,
    admin_secret: str,
) -> list[Route]:
    """Return Starlette Route objects for the /admin/tokens API."""

    async def create_token(request: Request) -> JSONResponse:
        if not _check_admin_auth(request, admin_secret):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        label: str | None = body.get("label")
        scopes: object = body.get("scopes", [])
        note: str | None = body.get("note")

        if not label:
            return JSONResponse({"error": "'label' is required"}, status_code=400)
        if not isinstance(scopes, list):
            return JSONResponse({"error": "'scopes' must be an array"}, status_code=400)

        entry = key_store.generate(
            user_id=label,
            scopes=scopes,
            description=note,
        )
        return JSONResponse(
            {
                "token_id": entry.id,
                "token_value": entry.token,
                "label": entry.user_id,
                "scopes": entry.scopes,
                "note": entry.description,
                "created_at": entry.created_at.isoformat(),
            },
            status_code=201,
        )

    async def revoke_token(request: Request) -> JSONResponse:
        if not _check_admin_auth(request, admin_secret):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        token_id: str = request.path_params["token_id"]
        ok = key_store.revoke(token_id)
        if ok:
            return JSONResponse({"revoked": True, "token_id": token_id})
        return JSONResponse(
            {"error": f"Token '{token_id}' not found or already revoked"},
            status_code=404,
        )

    async def list_tokens(request: Request) -> JSONResponse:
        if not _check_admin_auth(request, admin_secret):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        include_revoked = request.query_params.get("include_revoked", "").lower() in (
            "1",
            "true",
            "yes",
        )
        keys = key_store.list_keys(include_revoked=include_revoked)
        return JSONResponse(
            {
                "tokens": [
                    {
                        "token_id": k.id,
                        "label": k.user_id,
                        "scopes": k.scopes,
                        "note": k.description,
                        "created_at": k.created_at.isoformat(),
                        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
                    }
                    for k in keys
                ]
            }
        )

    return [
        Route("/admin/tokens", create_token, methods=["POST"]),
        Route("/admin/tokens/{token_id}", revoke_token, methods=["DELETE"]),
        Route("/admin/tokens", list_tokens, methods=["GET"]),
    ]
