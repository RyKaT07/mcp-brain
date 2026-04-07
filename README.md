# mcp-brain

Personal MCP server for persistent AI memory across sessions. Works with Claude Code, Cursor, and any MCP-compatible client.

## Features

- **Knowledge base** — Markdown files organized by scope (work/school/homelab), read and updated per-section with file locking
- **Inbox** — Staging area for scraped or proposed information, reviewed and accepted/rejected by you
- **Briefing** — Context loader from `meta.yaml` for session onboarding
- **Secrets schema** — Knows what credentials exist and where, never stores values
- **Multi-client** — SSE transport over HTTPS, works from anywhere

## Quick start

```bash
# Clone and install
git clone https://github.com/YOUR_USER/mcp-brain.git
cd mcp-brain
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Configure
cp .env.example .env           # edit with your token
cp knowledge/meta.yaml.example knowledge/meta.yaml  # edit with your info

# Initialize knowledge git tracking
cd knowledge && git init && cd ..

# Run
mcp-brain
```

## Connect from Claude Code

Add to your Claude Code MCP config (`~/.claude/claude_code_config.json` or project-level):

```json
{
  "mcpServers": {
    "brain": {
      "type": "sse",
      "url": "https://mcp.yourdomain.com/sse",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

## Deployment (Proxmox LXC)

1. Create Debian 12 LXC (1 vCPU, 512MB RAM, 8GB disk)
2. Install Python 3.12+, git, Caddy
3. Clone repo, install, configure `.env` and `meta.yaml`
4. Caddy reverse proxy: `yourdomain.com → localhost:8400`
5. Systemd service for auto-start

See `docs/deployment.md` for detailed setup.

## Architecture

```
Client (Claude Code / Cursor / custom)
  │ HTTPS + Bearer token
  ▼
Caddy (TLS termination)
  │
  ▼
mcp-brain (FastMCP, SSE transport)
  ├── knowledge/     ← markdown files, git-tracked
  ├── inbox/         ← pending items from scrapers
  └── meta.yaml      ← personal context & preferences
```

## Tools

| Tool | Description |
|------|-------------|
| `knowledge_read` | Read a knowledge file or section |
| `knowledge_update` | Update a specific section (section-level, not full overwrite) |
| `knowledge_list` | List available knowledge files |
| `inbox_list` | List pending inbox items |
| `inbox_add` | Add item to inbox |
| `inbox_accept` | Merge inbox item into knowledge |
| `inbox_reject` | Archive without merging |
| `get_briefing` | Load context from meta.yaml |
| `secrets_schema` | Look up credential structure (no values) |
