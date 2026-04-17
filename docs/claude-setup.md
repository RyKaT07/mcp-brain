# Claude setup

Connect your mcp-brain server to Claude — both the **Claude Code CLI** (local tool) and **claude.ai** cloud surfaces (web, iOS, Android, desktop app).

## Prerequisites

- mcp-brain running and reachable over HTTPS (e.g. `https://api.brainvlt.com`)
- For claude.ai (OAuth): `MCP_OAUTH_ADMIN_SECRET` set in your `.env` and server restarted
- For Claude Code CLI: a static bearer token in `data/auth.yaml`

---

## Option A — Claude Code CLI (bearer token, local use)

Claude Code connects via a static bearer token in your config file. No OAuth required.

### 1. Generate a token (skip if you already have one)

```bash
printf 'tok_%s\n' "$(openssl rand -hex 32)"
```

Add it to `data/auth.yaml`:

```yaml
tokens:
  - id: claude-code
    token: "tok_<your-token>"
    description: "Claude Code CLI — full access"
    scopes: ["*"]
```

Restart: `docker compose restart`

### 2. Configure Claude Code

Edit `~/.claude.json` (or a project-level `.claude.json`):

```json
{
  "mcpServers": {
    "brain": {
      "type": "http",
      "url": "https://api.brainvlt.com/mcp",
      "headers": {
        "Authorization": "Bearer tok_<your-token>"
      }
    }
  }
}
```

Replace `api.brainvlt.com` with your domain and `tok_<your-token>` with the actual token.

### 3. Verify

Restart Claude Code fully (`Cmd+Q` on macOS, or exit and re-launch on Linux) so the new config is picked up. The `knowledge_*`, `inbox_*`, `get_briefing`, and `secrets_schema` tools should appear in the session.

---

## Option B — claude.ai (web / iOS / Android / desktop)

claude.ai uses OAuth 2.0 Custom Connectors (remote MCP). mcp-brain runs a minimal OAuth 2.1 authorization server with Dynamic Client Registration — no pre-shared credentials needed.

The connector syncs across every surface signed into your Claude account, so you only set it up once.

### 1. Open Claude connectors

**Pro & Max plans (individual)**

`Customize` → `Connectors` → `+` → `Add custom connector`

**Team & Enterprise plans**

Owners: `Organization settings` → `Connectors` → `Add` → `Custom` → `Web`

Members: `Customize` → `Connectors` → find the connector → `Connect`

### 2. Enter the server URL

```
https://api.brainvlt.com/mcp
```

Replace `api.brainvlt.com` with your own domain if self-hosting.

Leave the OAuth Client ID and Client Secret fields **empty** — mcp-brain uses Dynamic Client Registration (DCR) to mint a fresh client automatically.

Click **Add**.

### 3. Approve the consent page

Your browser opens the mcp-brain consent page:

```
https://api.brainvlt.com/oauth/consent?pending=...
```

The page shows the requesting client name (`Claude` / `claude.ai`), a random client ID, and the scopes being granted. Enter your `MCP_OAUTH_ADMIN_SECRET` and click **Authorize**.

> If the consent page says "OAuth flow is not configured", `MCP_OAUTH_ADMIN_SECRET` is missing from your `.env`. Add it and `docker compose restart`.

### 4. Done

Claude redirects back and the connector appears as active. All mcp-brain tools are now available in every Claude surface signed into your account — no separate per-device setup.

### 5. Enable per conversation

In a new chat, click the **+** button → **Connectors** and toggle mcp-brain on. The tools (`knowledge_read`, `get_briefing`, etc.) then appear in that session.

---

## Token lifetimes

| Token         | Lifetime      | Rotation                                |
|---------------|---------------|-----------------------------------------|
| Access token  | 7 days        | claude.ai refreshes automatically       |
| Refresh token | No expiry     | Rotated on every refresh exchange       |

Bearer tokens (CLI) have no TTL unless you delete them from `auth.yaml`.

---

## Revoking access

**CLI** — remove the token from `data/auth.yaml` and restart.

**OAuth (claude.ai)** — stop the container, remove the Claude client from `data/oauth-state.json`, restart. Or clear all OAuth state:

```bash
rm data/oauth-state.json
docker compose restart
```

---

## Scopes

OAuth-issued tokens are always granted `*` (full access). Fine-grained scope restriction is enforced server-side via the write-policy in `knowledge/_meta/write-policy.md`, not at the OAuth layer. If you need a read-only Claude integration, generate a static yaml token with limited scopes and use it in Claude Code CLI directly — skip OAuth for that use case.

Full scope reference: [`auth.md`](auth.md).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| CLI: 401 Unauthorized | Token not in `auth.yaml` or typo | Check token value, restart container |
| CLI: `type: sse` deprecated | Old config format | Change to `"type": "http"` and URL to `/mcp` |
| Consent page → 503 | `MCP_OAUTH_ADMIN_SECRET` not set | Add to `.env`, restart |
| Consent page → 404 | Pending request expired (>5 min) | Start the flow again from claude.ai |
| Wrong secret error | Typo in admin secret | Re-enter exactly as in `.env` |
| Tools missing after connect | Claude cached the tool list | Disconnect and reconnect the connector |
| 401 on tool calls | Token expired and refresh failed | Revoke and re-authorize |
| Tools not in chat | Connector not toggled on | Click **+** → **Connectors** → enable |

## Differences from other clients

| Feature | Claude Code CLI | claude.ai (web/mobile) |
|---------|-----------------|------------------------|
| Auth method | Static bearer (`~/.claude.json`) | OAuth 2.1 (DCR + PKCE) |
| Token format | `tok_<hex>` | `tok_oauth_<hex>` (auto-minted) |
| Write policy injected | Yes (via `InitializeResult.instructions`) | Yes |
| Scope granularity | Per-token in `auth.yaml` | Always `*` (policy enforced server-side) |
| Per-device setup | Per machine | Once per Claude account |
