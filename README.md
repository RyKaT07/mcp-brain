# mcp-brain

Personal MCP server for persistent AI memory across sessions. Multi-token bearer auth with per-scope permissions, hosted as a Docker container in a Proxmox LXC, exposed via Caddy + DDNS. Works with Claude Code, Cursor, custom GPTs, n8n — any MCP-compatible client.

## Features

- **Knowledge base** — markdown with H2 sections, file-locked section-level updates, git auto-commit
- **Inbox** — staging area for scraped info, always human-reviewed before merge
- **Briefing** — context loader from `meta.yaml` for session bootstrap
- **Secrets schema** — knows what secrets exist and where, never knows the values
- **Multi-token auth** — different tokens grant different permissions (full / school-only / homelab read-only / etc.)
- **SSE over HTTPS** — works remotely, one Bearer header in the client's MCP config

## Quick start (Proxmox VE host)

One command on the Proxmox host shell:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/proxmox-install.sh)"
```

This creates a fresh unprivileged Debian LXC with nesting+keyctl enabled (required for Docker), then runs the in-container installer via `pct exec`. End result: a running mcp-brain on `127.0.0.1:8400` inside a new LXC, plus a printed admin bearer token and the LXC root password.

Override any default with env vars:

```bash
CTID=150 HOSTNAME=brain RAM_MB=2048 DISK_GB=16 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/proxmox-install.sh)"
```

If you already have a Debian/Ubuntu LXC or VM (or any Debian box, really), you can skip the wrapper and run the in-container installer directly:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/install.sh)
```

After either path, put Caddy/Traefik/nginx with TLS in front of port 8400. Full walkthrough: [`docs/deployment.md`](docs/deployment.md).

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
git clone https://github.com/RyKaT07/mcp-brain.git
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

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE). In plain terms:

- ✅ Personal use, hobby projects, research, education
- ✅ Non-profits, educational institutions, government
- ✅ Reading, studying, forking for your own non-commercial use
- ❌ Production use inside a for-profit company or commercial product
- ❌ Bundling into a paid service without a commercial license

If you or your company want to use mcp-brain commercially, open an issue
and we can talk about a commercial license. See [CONTRIBUTING.md](CONTRIBUTING.md)
for how pull requests are handled.

Use at your own risk — no warranty of any kind.
