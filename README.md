<div align="center">

<img src="https://brainvlt.com/logo.svg" alt="BrainVlt" width="80" height="80" />

# BrainVlt — mcp-brain

**Persistent AI memory you own and control.**

[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm%20NC-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/rykat07/mcp-brain)
[![MCP](https://img.shields.io/badge/Protocol-MCP-6366f1)](https://modelcontextprotocol.io)
[![brainvlt.com](https://img.shields.io/badge/website-brainvlt.com-a855f7)](https://brainvlt.com)

Give Claude, Cursor, and every other AI client a persistent memory that travels with you — self-hosted, version-controlled, and never uploaded to any third-party cloud.

[Website](https://brainvlt.com) · [Docs](docs/) · [Quick start](#quick-start-any-docker-host)

</div>

---

## What is it?

mcp-brain is a personal [Model Context Protocol](https://modelcontextprotocol.io) server. Every MCP-compatible AI client — Claude Code, claude.ai, Cursor, Windsurf, custom GPTs, n8n — can read from and write to a shared knowledge store as long as it holds a valid token.

Your notes stay in plain-text markdown files on a machine you control. A git repository inside the container tracks every change with a full audit trail. Nothing leaves your host.

---

## Features

| | Feature | Description |
|---|---------|-------------|
| 🧠 | **Knowledge base** | Markdown files with H2 sections; file-locked section-level updates; git auto-commit on every write |
| 📥 | **Inbox** | Staging area for scraped or draft info — always human-reviewed before it lands in the KB |
| ⚡ | **Briefing** | Session bootstrap: one call loads your personal context so the AI never starts cold |
| 🔑 | **Secrets schema** | Tells the AI which secrets exist and where; never stores the values themselves |
| 🔒 | **Multi-token auth** | Fine-grained per-scope bearer tokens — Claude Code gets `*`, Cursor gets `knowledge:read:school` |
| 🌐 | **OAuth 2.1 server** | DCR + PKCE + rotating refresh tokens so claude.ai Custom Connectors work without static headers |
| 📋 | **Todoist integration** | Read and create tasks; list projects and sections |
| 📅 | **Google Calendar** | Read events, create events, list calendars |
| 📝 | **Nextcloud Notes** | Browse and read your Nextcloud files |
| 🗂 | **Trello** | Read boards, lists, and cards; add cards |
| 📊 | **Structured logging** | JSON tool-call log per request: token id, tool name, duration, HTTP status |
| ♻️ | **Hot-reload auth** | Edit `auth.yaml` and tokens update within 5 s — no container restart |

---

## Supported AI clients

| Client | Transport | How to connect |
|--------|-----------|----------------|
| **Claude Code** (CLI) | Streamable HTTP | [`~/.claude.json` — bearer header](docs/claude-setup.md#option-a--claude-code-cli-bearer-token-local-use) |
| **claude.ai** (web / iOS / Android / desktop) | OAuth 2.1 | [Customize → Connectors → Add custom connector](docs/claude-setup.md#option-b--claudeai-web--ios--android--desktop) |
| **Cursor** | Streamable HTTP | MCP settings — bearer header |
| **Windsurf** | Streamable HTTP | MCP settings — bearer header |
| **ChatGPT / custom GPTs** | Streamable HTTP | Plugin manifest — bearer header |
| **n8n** | Streamable HTTP | MCP node — bearer header |
| **Any MCP client** | Streamable HTTP | One URL + one header |

---

## Quick start (any Docker host)

Runs on any Linux, macOS, or Windows machine with Docker Compose installed.

```bash
# 1. Grab the compose file and env template
curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/.env.example -o .env

# 2. Generate the required admin secret
echo "MCP_OAUTH_ADMIN_SECRET=$(openssl rand -hex 32)" >> .env

# 3. Create a minimal auth file
mkdir -p data
cat > data/auth.yaml <<'EOF'
tokens:
  - id: my-claude-code
    token: "tok_changeme_replace_with_openssl_rand_hex_32"
    scopes: ["*"]
EOF

# 4. Start
docker compose pull && docker compose up -d

# 5. Verify
curl http://127.0.0.1:8400/healthz
# → {"status":"ok"}
```

Port `8400` binds to `127.0.0.1` by default. Put Caddy, Traefik, or nginx with TLS in front before exposing it remotely. Full guide: [`docs/deployment.md`](docs/deployment.md).

---

## Quick start (Proxmox VE)

One command on the Proxmox host shell provisions a fresh Debian LXC with Docker and mcp-brain already running:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/proxmox-install.sh)"
```

Or on any existing Debian/Ubuntu box:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/install.sh)
```

Full walkthrough: [`docs/deployment.md`](docs/deployment.md).

---

## Connect from Claude Code

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "brain": {
      "type": "http",
      "url": "https://mcp.yourdomain.tld/mcp",
      "headers": {
        "Authorization": "Bearer tok_YOUR_TOKEN"
      }
    }
  }
}
```

## Connect from claude.ai (web / iOS / Android / desktop)

1. **Customize → Connectors → Add custom connector** (Pro/Max) or **Organization settings → Connectors** (Team/Enterprise)
2. **URL:** `https://mcp.yourdomain.tld/mcp`
3. Leave `OAuth Client ID` and `OAuth Client Secret` **empty** — Dynamic Client Registration handles this automatically
4. Click **Add** → your browser opens the consent page → enter your `MCP_OAUTH_ADMIN_SECRET` → **Authorize**
5. In a new chat, click **+** → **Connectors** and toggle mcp-brain on
6. Done. Works across every Claude surface on the same account.

Full setup guide: [`docs/claude-setup.md`](docs/claude-setup.md) · OAuth reference: [`docs/auth.md`](docs/auth.md).

---

## Auth — per-scope bearer tokens

Edit `/opt/mcp-brain/data/auth.yaml` (hot-reloads within 5 s):

```yaml
tokens:
  - id: claude-code-laptop
    token: "tok_..."
    scopes: ["*"]             # full access

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

Full scope grammar: [`docs/auth.md`](docs/auth.md).

---

## Tools

| Tool | Required scope |
|------|---------------|
| `knowledge_read` | `knowledge:read:<scope>` |
| `knowledge_update` | `knowledge:write:<scope>` |
| `knowledge_undo` | `knowledge:write:*` |
| `knowledge_list` | filtered by `knowledge:read:*` |
| `knowledge_map` | `knowledge:read:*` |
| `inbox_list` | `inbox:read` |
| `inbox_show` | `inbox:read` |
| `inbox_add` | `inbox:write` |
| `inbox_accept` | `inbox:write` + `knowledge:write:<target>` |
| `inbox_reject` | `inbox:write` |
| `get_briefing` | filtered by `briefing:<scope>` |
| `secrets_schema` | filtered by `secrets_schema:<scope>` |
| `todoist_list` | `todoist:read` |
| `todoist_add` | `todoist:write` |
| `gcal_events` | `gcal:read` |
| `gcal_add_event` | `gcal:write` |
| `trello_cards` | `trello:read` |
| `trello_add_card` | `trello:write` |
| `nextcloud_browse` | `nextcloud:read` |
| `brain_wake` | `wake:read` |

---

## Architecture

```
Claude Code / claude.ai / Cursor / ChatGPT / n8n
  │ HTTPS + Bearer token (or OAuth 2.1)
  ▼
Caddy (TLS termination)
  │
  ▼
mcp-brain  (FastMCP, Streamable HTTP)
  ├── /mcp            ← MCP endpoint
  ├── /healthz        ← Docker HEALTHCHECK
  ├── /oauth/*        ← OAuth 2.1 authorization server
  └── /admin/*        ← API key management (optional)

Data (bind-mounted volumes):
  ├── data/knowledge/   ← markdown KB, git-tracked
  ├── data/auth.yaml    ← bearer tokens (hot-reloaded)
  └── data/oauth-state.json
```

---

## Development (local, without Docker)

```bash
git clone https://github.com/RyKaT07/mcp-brain.git
cd mcp-brain
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp config/auth.yaml.example config/auth.yaml  # edit tokens
cp knowledge/meta.yaml.example knowledge/meta.yaml

# stdio mode — auth bypassed, great for local testing
MCP_TRANSPORT=stdio mcp-brain

# Streamable HTTP with auth (default)
MCP_AUTH_CONFIG=config/auth.yaml mcp-brain

# Run tests
pytest
```

---

## Write policy (optional)

If `knowledge/_meta/write-policy.md` exists, its contents are injected into the MCP `InitializeResult.instructions` and seen by every connected client. Use it to encode rules like "propose before writing" or "announce saves in a footer".

An optional `## Tool policy` H2 in the same file is prepended to the `knowledge_update` tool description — a second channel that reaches clients that silently ignore `instructions` (observed with claude.ai web).

Template: [`docs/write-policy.example.md`](docs/write-policy.example.md).

---

## Powered by BrainVlt

Using mcp-brain in your project? Add the badge:

```markdown
[![Powered by BrainVlt](https://brainvlt.com/badge.svg)](https://brainvlt.com)
```

[![Powered by BrainVlt](https://brainvlt.com/badge.svg)](https://brainvlt.com)

---

## Update

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update
```

Knowledge and tokens live in bind-mounted volumes — rollbacks are safe.

---

## License

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE).

- ✅ Personal use, hobby projects, research, education
- ✅ Non-profits, educational institutions, government
- ✅ Reading, studying, forking for personal non-commercial use
- ❌ Production use inside a for-profit company or commercial product
- ❌ Bundling into a paid service without a commercial license

Commercial licensing inquiries: open an issue. Contribution guidelines: [CONTRIBUTING.md](CONTRIBUTING.md).

Use at your own risk — no warranty of any kind.
