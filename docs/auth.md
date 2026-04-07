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

## What tokens see in logs

Plain uvicorn logs show:

- Client IP
- Request line (`GET /sse`, `POST /messages/...`)
- Status code (200, 401)

Beyond MVP: I plan to add structured logging with `tool name + token.id` per call. Minimal for now.

## Security and hardening

- **Bind to localhost only**: `docker-compose.yml` uses `127.0.0.1:8400` — terminate TLS in the reverse proxy.
- **`auth.yaml` chmod 600** — the installer does this automatically.
- **`data/knowledge` has its own git inside** — full history of every change, you can `git log` and recreate state from a week ago.
- **No HTTP `Server` header obfuscation** — single user, no point.
- **No rate limiting in MVP** — Caddy can do that if you want (`@limit { ... }`).
- **`secrets_schema` never returns values** — only key names + storage location (e.g. "1Password / vault: Work").
