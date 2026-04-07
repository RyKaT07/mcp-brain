# mcp-brain

Personal MCP server for persistent AI memory across sessions. Multi-token bearer auth z per-scope permissions, hostowany jako Docker container w Proxmox LXC, wystawiony przez Caddy + DDNS. Działa z Claude Code, Cursor, custom GPT, n8n — każdym MCP-compatible klientem.

## Features

- **Knowledge base** — markdown z H2 sekcjami, file-locked update per-section, git auto-commit
- **Inbox** — staging area dla scrapowanych info, zawsze human-review przed merge
- **Briefing** — context loader z `meta.yaml` na start sesji
- **Secrets schema** — wie jakie sekrety istnieją i gdzie, nigdy nie zna wartości
- **Multi-token auth** — różne tokeny = różne uprawnienia (full / school-only / homelab read-only / itd.)
- **SSE over HTTPS** — działa zdalnie, jeden Bearer header w MCP config klienta

## Quick start (Proxmox LXC)

```bash
# 1. Stwórz Debian 12 LXC w Proxmox (1 vCPU, 512MB RAM, 8GB disk, unprivileged + nesting)
# 2. SSH/pct enter, jako root:
bash <(curl -fsSL https://raw.githubusercontent.com/CHANGEME/mcp-brain/main/scripts/install.sh)
```

Installer:

- instaluje Dockera
- ściąga obraz `ghcr.io/CHANGEME/mcp-brain:latest`
- generuje pierwszy token (`tok_<32 hex>`) i wypisuje go raz
- startuje serwer na `127.0.0.1:8400`
- inicjalizuje `git` w `data/knowledge` (do auto-commit history)

Następnie postaw Caddy/Traefik/nginx z TLS przed portem 8400. Pełny walkthrough: [`docs/deployment.md`](docs/deployment.md).

## Update

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update
```

Trwa kilka sekund. Knowledge i tokeny są w volume mountach, więc rollback jest bezpieczny. Patrz [`docs/upgrade.md`](docs/upgrade.md).

## Connect from Claude Code

`~/.claude.json`:

```json
{
  "mcpServers": {
    "brain": {
      "type": "sse",
      "url": "https://mcp.yourdomain.tld/sse",
      "headers": {
        "Authorization": "Bearer tok_TWOJ_TOKEN"
      }
    }
  }
}
```

## Auth — multi-token z per-scope permissions

Edytuj `/opt/mcp-brain/data/auth.yaml`:

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

Po edycji: `cd /opt/mcp-brain && docker compose restart`. Pełna referencja scope grammar: [`docs/auth.md`](docs/auth.md).

## Tools

| Tool | Required scope |
|------|---------------|
| `knowledge_read` | `knowledge:read:<scope>` |
| `knowledge_update` | `knowledge:write:<scope>` |
| `knowledge_list` | filtr po `knowledge:read:*` |
| `inbox_list` | `inbox:read` |
| `inbox_add` | `inbox:write` |
| `inbox_accept` | `inbox:write` + `knowledge:write:<target>` |
| `inbox_reject` | `inbox:write` |
| `get_briefing` | filtr po `briefing:<scope>` |
| `secrets_schema` | filtr po `secrets_schema:<scope>` |

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

## Development (lokalnie, bez Dockera)

```bash
git clone https://github.com/CHANGEME/mcp-brain.git
cd mcp-brain
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
cp config/auth.yaml.example config/auth.yaml  # i edytuj tokeny
cp knowledge/meta.yaml.example knowledge/meta.yaml

# stdio mode bypasses auth — wygodne do testów z lokalnym MCP CLI
MCP_TRANSPORT=stdio mcp-brain

# albo SSE z auth
MCP_AUTH_CONFIG=config/auth.yaml mcp-brain
```

## Roadmap

Wszystko poza MVP czeka na osobne PRs — patrz `CLAUDE.md` i `docs/`.

1. Todoist tool (read+write)
2. Google Calendar tool
3. Obsidian vault integration (Syncthing → `data/knowledge/`)
4. Scrapery (university portal, Discord, IMAP) jako crony w LXC
5. REST wrapper (FastAPI) obok MCP dla nie-MCP klientów
6. Agent runner LXC (autonomiczne taski w tle)

## License

Personal project, no formal license. Use at your own risk; PRs welcome jeśli ktoś znajdzie to przydatne.
