# Upgrade

Update procedures for mcp-brain in Docker compose mode.

## Normal update (99% of cases)

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update
```

Or, if you symlinked it as `mcp-brain-update`:

```bash
sudo mcp-brain-update
```

What it does, in order:

1. Fetches the current upstream `docker-compose.yml` from `${MCP_BRAIN_REPO}@${MCP_BRAIN_BRANCH}` and re-pins the image tag to the installer's default.
2. Compares it against `/opt/mcp-brain/docker-compose.yml`:
   - **No change** → the existing file is kept as-is, no prompt.
   - **Change** → the diff is printed and you are prompted `Apply these compose changes? [y/N]`. Saying no aborts the update before the image is even pulled. Saying yes replaces `docker-compose.yml` with the new version and saves the previous one as `docker-compose.yml.bak` next to it.
3. `docker compose pull` — pulls the current image tag.
4. `docker compose up -d` — recreates the container with the (possibly updated) compose.
5. Prints `docker compose ps`.

The compose re-sync step exists because new releases occasionally add required env vars (the OAuth rollout added `MCP_OAUTH_ADMIN_SECRET`), and pulling a new image against a stale compose that does not wire the variable through is a silent failure mode. Always re-syncing — with a diff — catches that before it causes downtime.

For unattended upgrades (cron, CI), add `--yes` to auto-accept any compose diffs:

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update --yes
```

Takes a few seconds. The health check (`/healthz`) and `docker compose ps` are printed at the end.

### Restoring the previous compose file

If an upstream compose change breaks something in your environment, the previous version is right there next to the new one:

```bash
cd /opt/mcp-brain
mv docker-compose.yml.bak docker-compose.yml
docker compose up -d
```

Only the most recent backup is kept (each update overwrites the previous `.bak`), so if you want to roll further back, use git on the repo and edit by hand.

## Pinning a version

By default `docker-compose.yml` pulls the `latest` tag from GHCR — i.e. the latest build from `main`. If you prefer to pin a specific release:

```yaml
services:
  mcp-brain:
    image: ghcr.io/RyKaT07/mcp-brain:v0.2.0
```

Semver tags are published when git tags `v*.*.*` are pushed. List: https://github.com/RyKaT07/mcp-brain/pkgs/container/mcp-brain

## Rollback

```bash
cd /opt/mcp-brain
# find the previous image
docker images ghcr.io/RyKaT07/mcp-brain --format '{{.Tag}}\t{{.CreatedAt}}'
# pin it in compose and restart
sed -i 's|mcp-brain:latest|mcp-brain:sha-abc1234|' docker-compose.yml
docker compose up -d
```

Knowledge and `auth.yaml` are decoupled from the image (volume mount), so a rollback never eats your data.

## Breaking changes

If release notes mention a breaking change (e.g. a new required field in `auth.yaml`):

1. Before updating: `cp /opt/mcp-brain/data/auth.yaml /opt/mcp-brain/data/auth.yaml.bak`
2. `docker compose pull`
3. **Do not restart yet** — first edit `auth.yaml` to match the release notes
4. `docker compose up -d`
5. Verify `/healthz` and `docker compose logs`

In MVP the `auth.yaml` schema is frozen. Any change will be flagged as a semver minor bump plus a CHANGELOG entry.

## What survives an update

| Item                               | Survives | From                          |
|------------------------------------|----------|-------------------------------|
| `data/knowledge/`                  | yes      | bind mount                    |
| `data/knowledge/.git`              | yes      | bind mount                    |
| `data/auth.yaml`                   | yes      | bind mount                    |
| Config in image (`pyproject` etc.) | no       | regenerated from new image    |
| Previously working tokens          | yes      | stay in `auth.yaml`           |

## Updating the installer itself

The `install.sh` script lives at `/opt/mcp-brain/scripts/install.sh` after the first install — that is a copy from the moment of install. To pull a newer version:

```bash
sudo curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/install.sh \
  -o /opt/mcp-brain/scripts/install.sh
sudo chmod +x /opt/mcp-brain/scripts/install.sh
```

(Or: `sudo bash <(curl -fsSL ...install.sh)` — `cmd_install` is idempotent and will not overwrite your data or tokens.)
