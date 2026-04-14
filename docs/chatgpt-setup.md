# ChatGPT setup

Connect your mcp-brain server to ChatGPT using the Remote MCP integration (OAuth 2.0). Once connected, ChatGPT can call all your mcp-brain tools — knowledge, inbox, briefing, calendar, Todoist, Trello, and Nextcloud.

## Prerequisites

- mcp-brain running and reachable over HTTPS (e.g. `https://api.brainvlt.com`)
- `MCP_OAUTH_ADMIN_SECRET` set in your `.env` and server restarted
- ChatGPT desktop app (macOS or Windows) or ChatGPT.com with Connectors enabled

## Step-by-step

### 1. Open ChatGPT connectors

**Desktop app**

`Settings` → `Connectors` → `Add custom connector`

**ChatGPT.com (if available on your plan)**

`Settings` → `Connected apps` → `Add MCP server`

### 2. Enter the server URL

```
https://api.brainvlt.com/mcp
```

Replace `api.brainvlt.com` with your own domain if self-hosting.

Leave the OAuth Client ID and Client Secret fields **empty** — mcp-brain uses Dynamic Client Registration (DCR) to mint a fresh client automatically.

Click **Add** (or **Connect**).

### 3. Approve the consent page

Your browser opens the mcp-brain consent page:

```
https://api.brainvlt.com/oauth/consent?pending=...
```

The page shows the requesting client name, a random client ID, and the scopes being granted. Enter your `MCP_OAUTH_ADMIN_SECRET` and click **Authorize**.

> If the consent page says "OAuth flow is not configured", `MCP_OAUTH_ADMIN_SECRET` is missing from your `.env`. Add it and `docker compose restart`.

### 4. Done

ChatGPT redirects back and the connector appears as active. All mcp-brain tools are now available in every ChatGPT surface signed into your account.

## Token lifetimes

| Token         | Lifetime      | Rotation                            |
|---------------|---------------|-------------------------------------|
| Access token  | 7 days        | ChatGPT refreshes automatically     |
| Refresh token | No expiry     | Rotated on every refresh exchange   |

You only see the consent page once. Subsequent sessions are silent.

## Revoking access

To revoke ChatGPT's access:

1. Stop the container: `docker compose stop`
2. Edit `data/oauth-state.json` — remove the entry whose `client_name` is `ChatGPT` (or the matching `client_id`)
3. Remove any `access_tokens` and `refresh_tokens` with that `client_id`
4. `docker compose start`

Or nuke all OAuth state and re-authorize every client from scratch:

```bash
rm data/oauth-state.json
docker compose restart
```

## Scopes

OAuth-issued tokens are always granted `*` (full access). Fine-grained scope restriction is enforced server-side via the write-policy in `knowledge/_meta/write-policy.md`, not at the OAuth layer. If you need a read-only ChatGPT integration, generate a static yaml token with limited scopes and paste it directly — skip OAuth for that use case.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Consent page → 503 | `MCP_OAUTH_ADMIN_SECRET` not set | Add to `.env`, restart |
| Consent page → 404 | Pending request expired (>5 min) | Start the flow again from ChatGPT |
| Wrong secret error | Typo in admin secret | Re-enter exactly as in `.env` |
| Tools missing after connect | ChatGPT cached the tool list | Disconnect and reconnect the connector |
| 401 on tool calls | Token expired and refresh failed | Revoke and re-authorize |
