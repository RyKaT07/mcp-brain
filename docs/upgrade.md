# Upgrade

Update procedures dla mcp-brain w trybie Docker compose.

## Zwykły update (99% przypadków)

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update
```

Albo, jeśli zrobiłeś symlinka `mcp-brain-update`:

```bash
sudo mcp-brain-update
```

To po prostu:
```bash
cd /opt/mcp-brain
docker compose pull
docker compose up -d
```

Trwa kilka sekund. Health check (`/healthz`) i `docker compose ps` wypisują się na koniec.

## Pinning wersji

Domyślnie `docker-compose.yml` ciągnie tag `latest` z GHCR — czyli ostatni build z `main`. Jeśli wolisz pinować na konkretne release:

```yaml
services:
  mcp-brain:
    image: ghcr.io/CHANGEME/mcp-brain:v0.2.0
```

Tagi semver są publikowane przy pushu git tagów `v*.*.*`. Lista: https://github.com/CHANGEME/mcp-brain/pkgs/container/mcp-brain

## Rollback

```bash
cd /opt/mcp-brain
# znajdź poprzedni image
docker images ghcr.io/CHANGEME/mcp-brain --format '{{.Tag}}\t{{.CreatedAt}}'
# pinuj w compose i restart
sed -i 's|mcp-brain:latest|mcp-brain:sha-abc1234|' docker-compose.yml
docker compose up -d
```

Knowledge i auth.yaml są oddzielone od obrazu (volume mount), więc rollback nie zjada twoich danych.

## Breaking changes

Jeśli release notes wspomną o breaking change (np. nowy wymagany pole w `auth.yaml`):

1. Przed update: `cp /opt/mcp-brain/data/auth.yaml /opt/mcp-brain/data/auth.yaml.bak`
2. `docker compose pull`
3. **Nie restartuj jeszcze**, najpierw zaktualizuj `auth.yaml` zgodnie z release notes
4. `docker compose up -d`
5. Sprawdź `/healthz` i `docker compose logs`

W MVP `auth.yaml` schema jest zamrożony. Każda zmiana będzie sygnalizowana semver minor bumpem + wpisem w CHANGELOG.

## Co survive update

| Co                                  | Zostaje | Skąd                          |
|-------------------------------------|---------|-------------------------------|
| `data/knowledge/`                   | ✅      | bind mount                    |
| `data/knowledge/.git`               | ✅      | bind mount                    |
| `data/auth.yaml`                    | ✅      | bind mount                    |
| Config w obrazie (`pyproject` etc.) | ❌      | regenerowane z nowego obrazu  |
| Wcześniej działające tokeny         | ✅      | zostają w `auth.yaml`         |

## Update samego installera

Skrypt `install.sh` żyje w `/opt/mcp-brain/scripts/install.sh` po pierwszej instalacji — to KOPIA z momentu instalacji. Żeby pobrać nowszą wersję:

```bash
sudo curl -fsSL https://raw.githubusercontent.com/CHANGEME/mcp-brain/main/scripts/install.sh \
  -o /opt/mcp-brain/scripts/install.sh
sudo chmod +x /opt/mcp-brain/scripts/install.sh
```

(Albo: `sudo bash <(curl -fsSL ...install.sh)` — `cmd_install` jest idempotentny i nie nadpisze twoich danych ani tokenów.)
