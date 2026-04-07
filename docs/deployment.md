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
CTID=150 HOSTNAME=brain RAM_MB=2048 DISK_GB=16 \
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

1. `apt install` for missing packages (curl, openssl, xxd, ca-certificates)
2. Installs Docker via `https://get.docker.com` if it is not already there
3. Creates `/opt/mcp-brain/{,data,data/knowledge}`
4. Downloads `docker-compose.yml` from the repo and pins the image tag
5. Generates `tok_<32 hex>` and writes it into `/opt/mcp-brain/data/auth.yaml` (chmod 600)
6. `git init` inside `data/knowledge` (for auto-commit history)
7. `docker compose pull && up -d`
8. Probes `curl http://127.0.0.1:8400/healthz`
9. **Prints the token to the screen once** — save it to your password manager immediately

When it finishes, the server listens on `127.0.0.1:8400`. It is not yet exposed to the world.

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

## OPNsense — port forward

In OPNsense:

1. **Firewall → NAT → Port Forward**: redirect WAN:443 → your Caddy LXC IP:443
2. **Firewall → Rules → WAN**: allow 443/tcp from any (Let's Encrypt and clients)
3. **Services → Dynamic DNS**: configure DDNS for your domain

## Connect Claude Code

Edit `~/.claude.json` (or a project-level `.claude.json`):

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

Restart Claude Code and the `knowledge_*`, `inbox_*`, `get_briefing`, `secrets_schema` tools should be available.

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
- `/opt/mcp-brain/data/auth.yaml` — tokens (or just keep them in 1Password)

`docker-compose.yml` and the image are reproducible from the repo.

Easiest: an LXC snapshot in Proxmox once a week + a cron `restic backup /opt/mcp-brain/data/knowledge`.

## Troubleshooting

**`/healthz` returns 200 but `/sse` returns 401 with a valid token**
Check that the header is `Authorization: Bearer ` (with a space), not `Bearer:`. And that the token in the header is literally identical to the token in `auth.yaml`, no quotes.

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
