# Auth — tokens and scopes

mcp-brain uses multi-token bearer auth with per-token permission scopes. Each token has its own set of permissions; one token does not equal one client — you can have a separate token for your laptop running Claude Code (full access), another for Cursor that can only read school notes, another for n8n that can only read homelab + its secrets schema.

## Configuration

All tokens live in `/opt/mcp-brain/data/auth.yaml` (or wherever `MCP_AUTH_CONFIG` points). Format:

```yaml
tokens:
  - id: claude-code-laptop
    token: "tok_<32 hex>"
    description: "Personal laptop, full access"
    scopes: ["*"]

  - id: cursor-school
    token: "tok_<32 hex>"
    description: "Cursor session limited to coursework"
    scopes:
      - knowledge:read:school
      - knowledge:write:school
      - inbox:read
      - inbox:write
      - briefing:school
```

The `id` field is what shows up in logs ("token cursor-school accessed knowledge_read school/power-electronics"). The token value itself is never logged.

**After editing `auth.yaml`, restart the container** — there is no hot reload:
```bash
cd /opt/mcp-brain && docker compose restart
```

## Generating a token

```bash
printf 'tok_%s\n' "$(openssl rand -hex 32)"
```

The `tok_` prefix convention is optional but makes it easier to spot secrets in gists/diffs.

## Scope grammar

A three-segment grammar with `*` wildcards:

```
<resource>:<action>:<scope>
```

For resources where read/write distinction does not apply (inbox, briefing, secrets_schema) the second segment is collapsed:

```
<resource>:<scope>
```

| Tool                | Required (or filtered by)                             |
|---------------------|-------------------------------------------------------|
| `knowledge_read`    | `knowledge:read:<scope>`                              |
| `knowledge_update`  | `knowledge:write:<scope>`                             |
| `knowledge_undo`    | `knowledge:write:*` (full write — partial tokens cannot undo) |
| `knowledge_list`    | filters down to `<scope>` values the token can read   |
| `inbox_list`        | `inbox:read`                                          |
| `inbox_show`        | `inbox:read` (also reads from `_archive/`)            |
| `inbox_add`         | `inbox:write`                                         |
| `inbox_accept`      | `inbox:write` AND `knowledge:write:<target_scope>`    |
| `inbox_reject`      | `inbox:write`                                         |
| `get_briefing`      | filters sections to `briefing:<scope>` (or `*`)       |
| `secrets_schema`    | filters entries to `secrets_schema:<scope>` (or `*`)  |

### Wildcards

| Granted              | Matches                                          |
|----------------------|--------------------------------------------------|
| `*`                  | everything (god mode)                            |
| `knowledge:*:*`      | every knowledge operation, every scope           |
| `knowledge:read:*`   | read-only on knowledge, every scope              |
| `knowledge:read:work`| only knowledge_read on `work/`                   |

Wildcards only work when the segment count matches. `knowledge:*` does **not** match `knowledge:read:work` (different arity). The only exception is the bare `*`.

## Example roles

### Full access for yourself

```yaml
- id: claude-code-laptop
  token: "tok_..."
  scopes: ["*"]
```

### Cursor / collaborator — school only

```yaml
- id: cursor-student
  token: "tok_..."
  scopes:
    - knowledge:read:school
    - knowledge:write:school
    - inbox:read
    - inbox:write
    - briefing:school
```

Can read/write only `school/`. `knowledge_list()` will only show files under school. `get_briefing()` returns only the school section plus the preamble (timezone, preferences). They will not see `secrets_schema` at all.

### n8n homelab — read-only monitoring

```yaml
- id: n8n-homelab-ro
  token: "tok_..."
  scopes:
    - knowledge:read:homelab
    - briefing:homelab
    - secrets_schema:homelab
```

Can read homelab docs and view the homelab secrets schema (where things live, not the values themselves). No write access anywhere. Cannot see school or work.

### Custom GPT — briefing only

```yaml
- id: custom-gpt-briefer
  token: "tok_..."
  scopes:
    - briefing:work
    - briefing:school
```

Will only see briefings for those two scopes. Everything else returns permission denied.

### Discord bot — inbox only

```yaml
- id: discord-inbox-bot
  token: "tok_..."
  scopes:
    - inbox:write
```

Can drop suggestions into the inbox. Cannot accept them, cannot read them, cannot see knowledge.

## Token rotation

1. Generate a new one: `printf 'tok_%s\n' "$(openssl rand -hex 32)"`
2. Edit `data/auth.yaml`, add the new entry (do not delete the old one yet)
3. `docker compose restart`
4. Update the client to use the new token, verify it works
5. Delete the old entry, `restart` again

There is no deny list — removing a token from the YAML invalidates it the moment the container restarts.

## OAuth mode — claude.ai cloud surfaces

mcp-brain runs **two auth paths in parallel** through a single bearer
verifier. Both are equivalent from the tool's point of view — scopes
flow into `_perms.require()` the same way. The only difference is
*who holds the token* and *how it was minted*.

| Path                        | Used by                                   | Token shape                          | Minted by                    |
|-----------------------------|-------------------------------------------|--------------------------------------|------------------------------|
| **yaml bearer** (this doc)  | Claude Code CLI via `~/.claude.json`      | `tok_<hex>` — static, in `auth.yaml` | You, by hand                 |
| **OAuth-issued token**      | claude.ai web / iOS / Android / desktop   | `tok_oauth_<hex>` — dynamic          | mcp-brain's authorization server |

The split exists because claude.ai Custom Connectors (BETA) only
support OAuth 2.0; their dialog has no field for a custom
`Authorization` header. Rather than leaving those surfaces stranded,
mcp-brain ships a minimal single-user OAuth 2.1 authorization server
(DCR + authorization_code + PKCE + refresh, no JWT, no user database)
that wraps the same scope grammar.

### Setup

One environment variable on the LXC:

```bash
cd /opt/mcp-brain
echo "MCP_OAUTH_ADMIN_SECRET=$(openssl rand -hex 32)" >> .env
chmod 600 .env
docker compose up -d
```

Store `MCP_OAUTH_ADMIN_SECRET` in your password manager. It is the
secret that gates the consent page — without it, anyone who
discovered your `/oauth/consent` URL could approve themselves.
Fresh installs via `scripts/install.sh` auto-generate this on first
run and print it once alongside the yaml bearer token.

### Flow (what you actually do in claude.ai)

1. **claude.ai → Settings → Connectors → Add custom connector**
2. URL: `https://mcp.yourdomain.tld/mcp`
3. Leave `OAuth Client ID` and `OAuth Client Secret` empty (Dynamic
   Client Registration handles this automatically).
4. Click **Add**. Your browser is redirected to the mcp-brain
   consent page: `https://mcp.yourdomain.tld/oauth/consent?pending=...`.
5. The consent form shows the client name, a random client ID, and
   the scopes being granted (always `*` — OAuth-issued tokens are
   full-access at the protocol level; write discipline is enforced at
   the agent level via `knowledge/_meta/write-policy.md`).
6. Enter your `MCP_OAUTH_ADMIN_SECRET` and click **Authorize**.
7. Browser redirects back to claude.ai. Connector is active across
   every surface signed into your Claude account (web + iOS + Android
   + desktop chat) — no per-device setup.

First-use is the only time you see the consent page. claude.ai holds
a long-lived refresh token after that and silently exchanges it for
new access tokens every few days.

### Lifetimes and rotation

- **Access tokens** issued by the OAuth flow live for **7 days**
  (`ACCESS_TOKEN_TTL_S` in `mcp_brain/oauth.py`). claude.ai refreshes
  them automatically. Rotating the admin secret does not invalidate
  existing access tokens — it only changes what the consent page
  requires for NEW registrations.
- **Refresh tokens** have no TTL but rotate on every exchange: every
  refresh mints a brand-new refresh token and invalidates the old
  one. A leaked refresh token is good for exactly one use.
- **Registered clients** persist in `data/oauth-state.json` alongside
  the issued tokens. To manually revoke a claude.ai device: stop the
  server, edit `oauth-state.json` to remove the relevant client (and
  any access/refresh tokens with that `client_id`), restart.

### Relationship to yaml tokens

The two paths are completely independent but share the same
validator. At request time, `ChainedProvider.load_access_token()`
checks the OAuth store first and falls back to the yaml verifier on
a miss. So:

- Rotating a yaml token does not touch OAuth tokens.
- Rotating the OAuth admin secret does not touch yaml tokens.
- Removing `MCP_OAUTH_ADMIN_SECRET` breaks the OAuth consent page
  (returns 503) but yaml tokens keep working.
- Tools see `_perms._current_scopes()` exactly the same regardless
  of which path validated the incoming bearer.

### Why only `*` for OAuth-issued tokens

Yaml tokens can have fine-grained scopes (e.g. `knowledge:read:school`)
because you write them yourself. OAuth-issued tokens are minted on
the fly via DCR and we have no way to ask the user "what scope?" in
the middle of a browser redirect. So all OAuth-issued tokens get `*`
(full access), and the actual write discipline for wrist-slap-able
scopes (`health/`, `finance/`, `homelab/`) lives in the server-side
write-policy that every client sees in `InitializeResult.instructions`.
If you want tighter OAuth scopes, generate a yaml token with the
narrower scopes and use it in Claude Code CLI directly — skip OAuth
for that device entirely.

## What tokens see in logs

Plain uvicorn logs show:

- Client IP
- Request line (`POST /mcp`, etc.)
- Status code (200, 401)

Beyond MVP: I plan to add structured logging with `tool name + token.id` per call. Minimal for now.

## Security and hardening

- **Bind to localhost only**: `docker-compose.yml` uses `127.0.0.1:8400` — terminate TLS in the reverse proxy.
- **`auth.yaml` chmod 600** — the installer does this automatically.
- **`data/knowledge` has its own git inside** — full history of every change, you can `git log` and recreate state from a week ago.
- **No HTTP `Server` header obfuscation** — single user, no point.
- **No rate limiting in MVP** — Caddy can do that if you want (`@limit { ... }`).
- **`secrets_schema` never returns values** — only key names + storage location (e.g. "1Password / vault: Work").
