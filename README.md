# mcp-brain

Personal MCP server for persistent AI memory across sessions. Multi-token bearer auth with per-scope permissions, hosted as a Docker container in a Proxmox LXC, exposed via Caddy + DDNS. Works with Claude Code, Cursor, custom GPTs, n8n — any MCP-compatible client.

## Features

- **Knowledge base** — markdown with H2 sections, file-locked section-level updates, git auto-commit
- **Inbox** — staging area for scraped info, always human-reviewed before merge
- **Briefing** — context loader from `meta.yaml` for session bootstrap
- **Secrets schema** — knows what secrets exist and where, never knows the values
- **Multi-token auth** — different tokens grant different permissions (full / school-only / homelab read-only / etc.)
- **SSE over HTTPS** — works remotely, one Bearer header in the client's MCP config

## Quick start (Proxmox LXC)

```bash
# 1. Create a Debian 12 LXC in Proxmox (1 vCPU, 512MB RAM, 8GB disk, unprivileged + nesting)
# 2. SSH in or pct enter, then as root:
bash <(curl -fsSL https://raw.githubusercontent.com/CHANGEME/mcp-brain/main/scripts/install.sh)
```

The installer:

- installs Docker
- pulls `ghcr.io/CHANGEME/mcp-brain:latest`
- generates the first admin token (`tok_<32 hex>`) and prints it once
- starts the server on `127.0.0.1:8400`
- initializes a `git` repo inside `data/knowledge` (for auto-commit history)

After that, put Caddy/Traefik/nginx with TLS in front of port 8400. Full walkthrough: [`docs/deployment.md`](docs/deployment.md).

## Update

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update
```

Takes a few seconds. Knowledge and tokens live in volume mounts, so rollbacks are safe. See [`docs/upgrade.md`](docs/upgrade.md).

## Connect from Claude Code

`~/.claude.json`:

```json
{
  "mcpServers": {
    "brain": {
      "type": "sse",
      "url": "https://mcp.yourdomain.tld/sse",
      "headers": {
        "Authorization": "Bearer tok_YOUR_TOKEN"
      }
    }
  }
}
```

## Auth — multi-token with per-scope permissions

Edit `/opt/mcp-brain/data/auth.yaml`:

```yaml
tokens:
  - id: claude-code-laptop
    token: "tok_..."
    scopes: ["*"]  # full access

  - id: cursor-school
    token: "tok_..."
    scopes:
      - knowledge:read:school
      - knowledge:write:school
      - inbox:read
      - briefing:school

  - id: n8n-homelab-ro
    token: "tok_..."
    scopes:
      - knowledge:read:homelab
      - briefing:homelab
      - secrets_schema:homelab
```

After editing: `cd /opt/mcp-brain && docker compose restart`. Full scope grammar reference: [`docs/auth.md`](docs/auth.md).

## Tools

| Tool | Required scope |
|------|---------------|
| `knowledge_read` | `knowledge:read:<scope>` |
| `knowledge_update` | `knowledge:write:<scope>` |
| `knowledge_list` | filtered by `knowledge:read:*` |
| `inbox_list` | `inbox:read` |
| `inbox_show` | `inbox:read` |
| `inbox_add` | `inbox:write` |
| `inbox_accept` | `inbox:write` + `knowledge:write:<target>` |
| `inbox_reject` | `inbox:write` |
| `get_briefing` | filtered by `briefing:<scope>` |
| `secrets_schema` | filtered by `secrets_schema:<scope>` |

## Architecture

```
Client (Claude Code / Cursor / custom GPT / n8n)
  │ HTTPS + Bearer token
  ▼
Caddy (TLS termination, no auth)
  │
  ▼
mcp-brain (FastMCP, SSE, multi-token bearer auth)
  ├── /data/knowledge/          ← markdown KB, git-tracked, bind-mounted
  ├── /data/auth.yaml           ← bearer tokens (read-only mount)
  └── meta.yaml in knowledge/   ← user profile + secrets_schema
```

## Development (local, without Docker)

```bash
git clone https://github.com/CHANGEME/mcp-brain.git
cd mcp-brain
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
cp config/auth.yaml.example config/auth.yaml  # then edit tokens
cp knowledge/meta.yaml.example knowledge/meta.yaml

# stdio mode bypasses auth — handy for local testing with the MCP CLI
MCP_TRANSPORT=stdio mcp-brain

# or SSE with auth
MCP_AUTH_CONFIG=config/auth.yaml mcp-brain
```

## Roadmap

Everything beyond MVP waits for separate PRs — see `CLAUDE.md` and `docs/`.

1. Todoist tool (read+write)
2. Google Calendar tool
3. Obsidian vault integration (Syncthing → `data/knowledge/`)
4. Scrapers (university portal, Discord, IMAP) as LXC crons
5. REST wrapper (FastAPI) alongside MCP for non-MCP clients
6. Agent runner LXC (autonomous background tasks)

## License

Personal project, no formal license. Use at your own risk; PRs welcome if anyone finds it useful.
