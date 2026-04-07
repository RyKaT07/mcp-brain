# Deployment — Proxmox LXC

Krok po kroku jak postawić mcp-brain w LXC na Proxmoxie i wystawić go przez Caddy + DDNS. Zajmuje to ~10 minut na świeżym kontenerze.

## TL;DR (jeśli już wiesz co robić)

```bash
# wewnątrz świeżego Debian 12 LXC, jako root:
bash <(curl -fsSL https://raw.githubusercontent.com/CHANGEME/mcp-brain/main/scripts/install.sh)
```

Skrypt zainstaluje Dockera, ściągnie obraz z GHCR, wygeneruje token i wystartuje serwer na `127.0.0.1:8400`. Reverse proxy (Caddy) zostawiamy tobie — patrz sekcja niżej.

---

## 1. Stwórz LXC w Proxmoxie

W Proxmox UI → `Create CT`:

| Pole              | Wartość                                                |
|-------------------|--------------------------------------------------------|
| Hostname          | `mcp-brain`                                            |
| Template          | `debian-12-standard` (Debian 12 / Ubuntu 24.04 też OK) |
| Disk              | 8 GB                                                   |
| CPU cores         | 1                                                      |
| RAM               | 512 MB (768 MB jeśli chcesz zapas)                     |
| Network           | DHCP albo statyczny w twojej VLAN                      |
| Unprivileged      | ✅ tak                                                  |
| Nesting / keyctl  | ✅ tak (Options → Features) — wymagane przez Dockera   |

Po starcie zrób `pct enter <id>` lub SSH do kontenera.

## 2. Odpal installer

Wewnątrz LXC, jako root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CHANGEME/mcp-brain/main/scripts/install.sh)
```

Co robi skrypt:

1. `apt install` brakujących pakietów (curl, openssl, xxd, ca-certificates)
2. Instaluje Dockera przez `https://get.docker.com` jeśli nie ma
3. Tworzy `/opt/mcp-brain/{,data,data/knowledge}`
4. Pobiera `docker-compose.yml` z repo i pinuje image tag
5. Generuje `tok_<32 hex>` i zapisuje do `/opt/mcp-brain/data/auth.yaml` (chmod 600)
6. `git init` w `data/knowledge` (do auto-commit history)
7. `docker compose pull && up -d`
8. Sprawdza `curl http://127.0.0.1:8400/healthz`
9. **Wypisuje token raz na ekran** — zapisz go w 1Password od razu

Po skończeniu serwer słucha na `127.0.0.1:8400`. Nie jest jeszcze wystawiony na świat.

## 3. Reverse proxy (Caddy)

Zalecane: osobny LXC "edge" z Caddy, który obsługuje wszystkie twoje self-hosted serwisy. Albo Caddy w tym samym kontenerze co mcp-brain — twój wybór.

Minimalny `Caddyfile`:

```caddyfile
mcp.yourdomain.tld {
    reverse_proxy 10.0.0.42:8400
}
```

Caddy automatycznie ogarnie Let's Encrypt jeśli DNS wskazuje na twój publiczny IP. **Bearer token sprawdza sam mcp-brain**, więc Caddy nie musi nic dodatkowo robić.

Pełniejszy przykład w [`Caddyfile.example`](Caddyfile.example).

## 4. OPNsense — port forward

W OPNsense:

1. **Firewall → NAT → Port Forward**: redirect WAN:443 → IP twojego Caddy LXC:443
2. **Firewall → Rules → WAN**: pozwól na 443/tcp z any (LE i klienty)
3. **Services → Dynamic DNS**: skonfiguruj DDNS na swoją domenę

## 5. Podłącz Claude Code

Edytuj `~/.claude.json` (albo project-level `.claude.json`):

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

Restart Claude Code i tooli `knowledge_*`, `inbox_*`, `get_briefing`, `secrets_schema` powinny być dostępne.

## 6. Update

```bash
sudo bash /opt/mcp-brain/scripts/install.sh update
```

Albo (jeśli zrobiłeś symlinka):

```bash
sudo mcp-brain-update
```

To po prostu `docker compose pull && up -d`. Trwa ~10 sekund. Patrz [`upgrade.md`](upgrade.md) jeśli kiedyś release notes wspomną o breaking changes.

## 7. Backup

To co warto backupować z LXC:

- `/opt/mcp-brain/data/knowledge/` — twoja KB (sam katalog + jego `.git`)
- `/opt/mcp-brain/data/auth.yaml` — tokeny (lub po prostu trzymaj je w 1Password)

`docker-compose.yml` i obraz są reproducible z repo.

Najprościej: snapshot LXC w Proxmoxie raz na tydzień + cron `restic backup /opt/mcp-brain/data/knowledge`.

## Troubleshooting

**`/healthz` zwraca 200 ale `/sse` daje 401 z prawidłowym tokenem**
Sprawdź `Authorization: Bearer ` (z spacją), nie `Bearer:`. I że token w nagłówku == token w `auth.yaml` (literalnie, bez cudzysłowów).

**Docker startuje ale nie działa w LXC (cgroup error)**
Włącz `Nesting` w opcjach kontenera (Proxmox → CT → Options → Features). Restart LXC.

**Knowledge nie commituje się**
Sprawdź że `data/knowledge/.git` istnieje i jest własnością UID 1000:
```bash
ls -la /opt/mcp-brain/data/knowledge/.git
chown -R 1000:1000 /opt/mcp-brain/data/knowledge
```

**Dodanie nowego tokena nie działa**
Po edycji `data/auth.yaml`: `cd /opt/mcp-brain && docker compose restart`. Brak hot-reloadu (świadomie — single-user, restart jest tani).
