# Gemini setup

Connect your mcp-brain server to Gemini using the MCP integration. Two surfaces are supported: **Gemini CLI** (local tool) and **Google AI Studio** / **Gemini for Google Workspace** (cloud surfaces via OAuth).

## Prerequisites

- mcp-brain running and reachable over HTTPS (e.g. `https://api.brainvlt.com`)
- For OAuth surfaces: `MCP_OAUTH_ADMIN_SECRET` set in your `.env` and server restarted
- For Gemini CLI: `gemini` CLI installed (`npm install -g @google/gemini-cli` or equivalent)

---

## Option A — Gemini CLI (bearer token, local use)

Gemini CLI connects to MCP servers via a config file. No OAuth required — use a static bearer token from your `auth.yaml`.

### 1. Generate a token (skip if you already have one)

```bash
printf 'tok_%s\n' "$(openssl rand -hex 32)"
```

Add it to `data/auth.yaml`:

```yaml
tokens:
  - id: gemini-cli
    token: "tok_<your-token>"
    description: "Gemini CLI — full access"
    scopes: ["*"]
```

Restart: `docker compose restart`

### 2. Configure Gemini CLI

Edit (or create) `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "mcp-brain": {
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

```bash
gemini
```

In the session, mcp-brain tools should appear in the tool list. Test with:

```
> brain_wake
```

---

## Option B — Google AI Studio / Gemini for Workspace (OAuth)

Cloud surfaces use OAuth 2.0. The flow is identical to the [ChatGPT setup](chatgpt-setup.md) — DCR + authorization_code + PKCE.

### 1. Open Gemini connectors

**Google AI Studio** (`aistudio.google.com`)

`Settings` → `Integrations` → `MCP servers` → `Add server`

**Gemini for Google Workspace**

`Settings` → `Extensions` → `Connect MCP server`

> Navigation labels vary by rollout version. Look for "MCP", "Connectors", or "Extensions".

### 2. Enter the server URL

```
https://api.brainvlt.com/mcp
```

Leave the OAuth credential fields empty — mcp-brain auto-registers via DCR.

Click **Connect**.

### 3. Approve the consent page

Your browser opens the mcp-brain consent page:

```
https://api.brainvlt.com/oauth/consent?pending=...
```

Enter your `MCP_OAUTH_ADMIN_SECRET` and click **Authorize**.

### 4. Done

The connector becomes active across all Gemini surfaces in your Google account.

---

## Token lifetimes

| Token         | Lifetime      | Rotation                                |
|---------------|---------------|-----------------------------------------|
| Access token  | 7 days        | Gemini refreshes automatically          |
| Refresh token | No expiry     | Rotated on every exchange               |

Bearer tokens (CLI) have no TTL unless you delete them from `auth.yaml`.

---

## Revoking access

**CLI** — remove the token from `data/auth.yaml` and restart.

**OAuth (AI Studio / Workspace)** — stop the container, remove the Gemini client from `data/oauth-state.json`, restart. Or clear all OAuth state:

```bash
rm data/oauth-state.json
docker compose restart
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| CLI: 401 Unauthorized | Token not in `auth.yaml` or typo | Check token value, restart container |
| CLI: connection refused | Server not running or wrong URL | `curl https://api.brainvlt.com/healthz` |
| Consent page → 503 | `MCP_OAUTH_ADMIN_SECRET` not set | Add to `.env`, restart |
| Consent page → 404 | Pending expired (>5 min) | Start flow again from Gemini |
| Tools not listed | Gemini cached empty tool list | Disconnect and reconnect |

## Differences from Claude Code CLI

| Feature | Claude Code CLI | Gemini CLI |
|---------|-----------------|------------|
| Auth method | Static bearer (`~/.claude.json`) | `Authorization` header in `settings.json` |
| Token format | `tok_<hex>` | Same |
| Write policy injected | Yes (via `InitializeResult.instructions`) | Depends on client MCP implementation |
| OAuth support | Yes (claude.ai surfaces) | Yes (AI Studio / Workspace) |

If Gemini CLI does not display the server's write policy at session start, it means the client does not expose `InitializeResult.instructions` to the model. The policy is still enforced server-side — only the user-facing reminder is missing.
