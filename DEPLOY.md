# Deployment Guide — mcp-brain

## Branch Workflow

| Branch    | Environment | Docker tag | Auto-deploy |
|-----------|-------------|------------|-------------|
| `develop` | dev         | `:dev`     | Yes (on push) |
| `main`    | prod        | `:latest`  | Yes (on push) |

Pushing to either branch triggers the GitHub Actions workflow (`.github/workflows/docker.yml`) which builds the multi-arch image (amd64 + arm64), pushes it to GHCR, then SSH-deploys it to the target server.

Tags on `main` (`v*.*.*`) trigger a versioned build but not an auto-deploy.

## Required GitHub Secrets

Configure these in `Settings → Secrets → Actions` on the repository:

| Secret              | Description                                      |
|---------------------|--------------------------------------------------|
| `SSH_PRIVATE_KEY`   | Private key for SSH access to deployment servers |
| `SSH_USER`          | SSH username (e.g. `root`)                       |
| `PROD_SERVER_HOST`  | Hostname or IP of the production server          |
| `PROD_DEPLOY_PATH`  | Absolute path to the deploy directory on prod    |
| `DEV_SERVER_HOST`   | Hostname or IP of the dev server                 |
| `DEV_DEPLOY_PATH`   | Absolute path to the deploy directory on dev     |

## Manual Deployment with Compose Overrides

Pull the latest image and start/restart the service for the target environment:

```bash
# Production
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Dev
docker compose -f docker-compose.yml -f docker-compose.dev.yml pull
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

The override files (`docker-compose.prod.yml`, `docker-compose.dev.yml`) only set the image tag — all other config comes from the base `docker-compose.yml` and your `.env` file.

## Promoting develop → main

1. Open a pull request from `develop` → `main` on GitHub.
2. Review and merge the PR.
3. The `main` push triggers the prod build + deploy automatically.

Do **not** push directly to `main`. Always promote through a PR so the build is tested first.

## Notes

- `.env` files are never committed — copy `.env.example` on each server and fill in secrets.
- Base `docker-compose.yml` is not modified by deploys — it is the production base config.
- Data volumes (`./data`) are bind-mounted and persist across restarts; back them up before major updates.
- Rollback: re-run the deploy workflow for a previous SHA tag, or manually `docker compose up -d` with a pinned image tag in your `.env`.
