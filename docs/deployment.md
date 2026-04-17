# Deployment — Proxmox LXC

Two supported paths:

- **One-liner on the Proxmox host** (recommended): a single script creates the LXC and installs mcp-brain in one go.
- **Two-step** (fallback): create the LXC yourself, then run `scripts/install.sh` inside it.

Either way, after the container is up you still need a reverse proxy with TLS in front — see the Caddy section below.

## Option A — one-liner on the Proxmox host (recommended)

Run this **on your Proxmox VE host shell** (Datacenter → your node → Shell in the web UI, or SSH into the host). Not inside an existing LXC:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/proxmox-install.sh)"
```

What it does:

1. Sanity-checks that you are actually on a Proxmox VE host (`pveversion`, `pct`, `pveam`, `pvesm` present)
2. Picks the next free CTID, downloads the latest `debian-13-standard` template if missing
3. Creates an **unprivileged** LXC with **nesting+keyctl** features (required for Docker inside), default sizing: 1 core / 1 GB RAM / 8 GB disk on the first storage that supports `rootdir`, DHCP on `vmbr0`
4. Shows the resolved configuration and asks for confirmation (skip with `ASSUME_YES=1`)
5. Starts the container, waits for network
6. Runs `scripts/install.sh` inside the LXC via `pct exec` — that installs Docker, pulls the image from GHCR, generates the admin token, starts the server
7. Reads the token back out of `/data/auth.yaml` and prints it together with the LXC IP and the generated root password

All defaults can be overridden via env vars before the `bash -c`:

```bash
CTID=150 CT_HOSTNAME=brain RAM_MB=2048 DISK_GB=16 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/proxmox-install.sh)"
```

Full list of env vars is at the top of `scripts/proxmox-install.sh`.

When it finishes, you will see the generated root password and mcp-brain bearer token **printed once**. Save them to your password manager immediately — neither is stored anywhere the script can re-print later (the password is not in `auth.yaml`, and the token is in `auth.yaml` but the admin is expected to rotate it eventually).

Then skip to [Reverse proxy (Caddy)](#reverse-proxy-caddy).

## Option B — two-step (manual LXC + in-container installer)

Use this if you already have an LXC (for example created by [community-scripts Docker LXC](https://community-scripts.github.io/ProxmoxVE/scripts?id=docker)) or if the one-liner fails for any reason.

### 1. Create the LXC in Proxmox

In Proxmox UI → `Create CT`:

| Field             | Value                                                  |
|-------------------|--------------------------------------------------------|
| Hostname          | `mcp-brain`                                            |
| Template          | `debian-13-standard` (Debian 12 / Ubuntu 24.04 also OK)|
| Disk              | 8 GB                                                   |
| CPU cores         | 1                                                      |
| RAM               | 1024 MB                                                |
| Network           | DHCP or a static IP in your VLAN                       |
| Unprivileged      | yes                                                    |
| Nesting / keyctl  | yes (Options → Features) — required by Docker         |

After it boots, `pct enter <id>` or SSH into the container.

### 2. Run the installer inside the LXC

Inside the LXC, as root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/install.sh)
```

What it does:

1. `apt install` for missing packages (curl, openssl, xxd, ca-certificates, git)
2. Installs Docker via `https://get.docker.com` if it is not already there
3. Creates `/opt/mcp-brain/{,data,data/knowledge}`
4. Downloads `docker-compose.yml` from the repo and pins the image tag
5. Generates `tok_<32 hex>` and writes it into `/opt/mcp-brain/data/auth.yaml` (chmod 600)
6. Generates a 256-bit OAuth admin secret and writes it into `/opt/mcp-brain/.env` as `MCP_OAUTH_ADMIN_SECRET` (required by the OAuth 2.1 authorization server that claude.ai Custom Connectors go through — see [`auth.md`](auth.md#oauth-mode))
7. `git init` inside `data/knowledge` (for auto-commit history)
8. `docker compose pull && up -d`
9. Probes `curl http://127.0.0.1:8400/healthz`
10. **Prints the bearer token AND the OAuth admin secret to the screen once** — save both to your password manager immediately

When it finishes, the server listens on `127.0.0.1:8400`. It is not yet exposed to the world.

### Upgrading an existing install to the OAuth flow

If you installed mcp-brain before the OAuth 2.1 authorization server existed, your `/opt/mcp-brain/.env` file does not have `MCP_OAUTH_ADMIN_SECRET` yet and `docker compose up` will now refuse to start until you add one. Generate and append it:

```bash
cd /opt/mcp-brain
echo "MCP_OAUTH_ADMIN_SECRET=$(openssl rand -hex 32)" >> .env
chmod 600 .env
docker compose pull
docker compose up -d
```

Save the generated secret to your password manager. It is the secret you will enter on the consent page the first time you add a Custom Connector in claude.ai. Claude Code CLI (`~/.claude.json` with a static bearer) keeps working whether the admin secret is set or not — it goes through a different auth path.

## Reverse proxy (Caddy)

Recommended: a separate "edge" LXC running Caddy that fronts all your self-hosted services. Or run Caddy in the same container as mcp-brain — your call.

Minimal `Caddyfile`:

```caddyfile
mcp.yourdomain.tld {
    reverse_proxy 10.0.0.42:8400
}
```

Caddy handles Let's Encrypt automatically as long as DNS points at your public IP. **The bearer token is checked by mcp-brain itself**, so Caddy does not need to do anything extra.

A fuller example lives in [`Caddyfile.example`](Caddyfile.example).

### Port binding — same-host vs cross-host Caddy

`docker-compose.yml` exposes port 8400 through the `MCP_PORT_BIND` env
var (defaults to `127.0.0.1`). Which value you want depends on where
your reverse proxy lives **relative to this LXC**:

| Your setup                                                       | `MCP_PORT_BIND` in `.env` | Why                                                                                                |
|------------------------------------------------------------------|---------------------------|----------------------------------------------------------------------------------------------------|
| Caddy/Traefik/nginx runs **in the same LXC** as mcp-brain        | unset (→ `127.0.0.1`)     | Default. Safest. The port is only reachable from inside the LXC; nothing on the LAN can hit 8400. |
| Caddy runs in a **separate LXC** and forwards to this LXC's IP   | `0.0.0.0`                 | The bind has to listen on the LAN interface for cross-LXC traffic to reach it.                    |
| Caddy runs on the **Proxmox host** and forwards to the LXC's IP  | `0.0.0.0`                 | Same reason — host-to-LXC traffic uses the LAN IP, not `127.0.0.1`.                               |

When you switch to `0.0.0.0`, **restrict access at the firewall level**
(OPNsense rule, `iptables` on the host, etc.) — mcp-brain's bearer
auth alone is not meant to be internet-facing. Only Caddy's IP should
be allowed to reach port 8400.

To apply a change:

```bash
cd /opt/mcp-brain
vi .env                               # MCP_PORT_BIND=0.0.0.0
docker compose down && docker compose up -d
```

`docker compose restart` is NOT enough — docker-compose only re-reads
`.env` on `up`. If you flip `MCP_PORT_BIND` and nothing seems to
change, that is almost always the reason.

## OPNsense — port forward

In OPNsense:

1. **Firewall → NAT → Port Forward**: redirect WAN:443 → your Caddy LXC IP:443
2. **Firewall → Rules → WAN**: allow 443/tcp from any (Let's Encrypt and clients)
3. **Services → Dynamic DNS**: configure DDNS for your domain

## Connect Claude Code (laptop CLI)

Edit `~/.claude.json` (or a project-level `.claude.json`):

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

mcp-brain uses the Streamable HTTP transport. The public endpoint is
`/mcp` on your domain; `/healthz` is unauthenticated and useful for
uptime checks. Restart Claude Code fully (`Cmd+Q` on macOS) after
editing `~/.claude.json` so the new config is picked up; the
`knowledge_*`, `inbox_*`, `get_briefing`, and `secrets_schema` tools
should then be visible in the session.

## Connect claude.ai (web / iOS / Android / desktop chat)

claude.ai Custom Connectors (BETA) do not support static bearer
headers — they speak OAuth 2.0. mcp-brain runs a minimal OAuth 2.1
authorization server on the same domain to handle them. The setup
is one-time per account (not per device — the connector syncs from
your Claude account to every surface you sign in to).

**Prerequisite:** `MCP_OAUTH_ADMIN_SECRET` is set in `/opt/mcp-brain/.env`
on the LXC (the installer generates it on fresh installs; see
[Upgrading an existing install](#upgrading-an-existing-install-to-the-oauth-flow)
for how to add it to an older install).

1. Sign in at [claude.ai](https://claude.ai) → **Customize → Connectors → Add custom connector** (Pro/Max plans) or **Organization settings → Connectors → Add** (Team/Enterprise)
2. **Name:** `mcp-brain` (or whatever you prefer)
3. **Remote MCP server URL:** `https://mcp.yourdomain.tld/mcp`
4. **Leave OAuth Client ID and OAuth Client Secret EMPTY** — mcp-brain
   supports Dynamic Client Registration (RFC 7591), so claude.ai will
   register itself automatically on first use. Pre-filled credentials
   would confuse the flow.
5. Click **Add**. Your browser is redirected to `https://mcp.yourdomain.tld/oauth/consent?pending=...`
6. The consent page shows the client name (`Claude` / `claude.ai`), a
   random client ID, and a password field. Enter your `MCP_OAUTH_ADMIN_SECRET`
   and click **Authorize**.
7. Browser redirects back to claude.ai; the connector shows as active.
8. Open a new chat and ask for something that should trigger an
   mcp-brain tool ("what's in my briefing", "search my knowledge
   for foo"). The tools appear in the session.

Any device signed into the same Claude account (iOS, Android, desktop
chat app) picks up the connector automatically — no separate setup
per device. claude.ai stores a refresh token and quietly exchanges it
for new access tokens in the background, so you only see the consent
page the very first time (or after you explicitly disconnect and
re-add the connector).

**Rotating the admin secret:** edit `/opt/mcp-brain/.env`, replace
`MCP_OAUTH_ADMIN_SECRET=...`, `docker compose restart`. Existing
OAuth-issued tokens keep working (the secret only gates the consent
page for new registrations). Full scope reference and the rationale
behind this split-brain auth model are in [`auth.md`](auth.md).

Dedicated setup guide with troubleshooting: [`claude-setup.md`](claude-setup.md).

## Update

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update
```

Or, if you symlinked it:

```bash
sudo mcp-brain-update
```

That is just `docker compose pull && up -d`. Takes ~10 seconds. See [`upgrade.md`](upgrade.md) if release notes ever mention breaking changes.

## Backup

Worth backing up from the LXC:

- `/opt/mcp-brain/data/knowledge/` — your KB (the directory plus its `.git`)
- `/opt/mcp-brain/data/auth.yaml` — yaml bearer tokens (or just keep them in 1Password)
- `/opt/mcp-brain/data/oauth-state.json` — OAuth-issued tokens + registered claude.ai clients. Losing this just means every claude.ai device needs to re-run the Custom Connector setup flow; it is not a disaster, but backing it up avoids the consent-page step after a restore.
- `/opt/mcp-brain/.env` — includes `MCP_OAUTH_ADMIN_SECRET`. Treat like `auth.yaml`: password manager + off-host backup.

`docker-compose.yml` and the image are reproducible from the repo.

Easiest: an LXC snapshot in Proxmox once a week + a cron `restic backup /opt/mcp-brain/data/knowledge`.

## Troubleshooting

**`/healthz` returns 200 but `/mcp` returns 401 with a valid token**
Check that the header is `Authorization: Bearer ` (with a space), not `Bearer:`. And that the token in the header is literally identical to the token in `auth.yaml`, no quotes.

**Client says `type: sse` server is deprecated or tools do not load**
mcp-brain migrated from SSE to Streamable HTTP transport. Update your client config to use `"type": "http"` and point the URL at `/mcp` (not `/sse`). SSE is dead in the MCP spec as of 2026.

**Docker starts but does not work in the LXC (cgroup error)**
Enable `Nesting` in container options (Proxmox → CT → Options → Features). Restart the LXC.

**Knowledge does not auto-commit**
Check that `data/knowledge/.git` exists and is owned by UID 1000:
```bash
ls -la /opt/mcp-brain/data/knowledge/.git
chown -R 1000:1000 /opt/mcp-brain/data/knowledge
```

**Adding a new token does not take effect**
After editing `data/auth.yaml`: `cd /opt/mcp-brain && docker compose restart`. There is no hot-reload (deliberately — single user, restart is cheap).
