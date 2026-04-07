# Auth — tokeny i scopes

mcp-brain używa wielo-tokenowego bearer auth z per-token permission scopes. Każdy token ma swój zestaw uprawnień; jeden token ≠ jeden klient — możesz mieć osobny token na laptopa do Claude Code (full access), osobny do Cursor który ma tylko czytać studia, osobny do n8n który ma czytać tylko homelab + jego sekrety.

## Konfiguracja

Wszystkie tokeny żyją w `/opt/mcp-brain/data/auth.yaml` (albo gdziekolwiek wskazuje `MCP_AUTH_CONFIG`). Format:

```yaml
tokens:
  - id: claude-code-laptop
    token: "tok_<32 hex>"
    description: "Personal laptop, full access"
    scopes: ["*"]

  - id: cursor-school
    token: "tok_<32 hex>"
    description: "Cursor session limited to coursework"
    scopes:
      - knowledge:read:school
      - knowledge:write:school
      - inbox:read
      - inbox:write
      - briefing:school
```

Pole `id` służy do logowania ("token cursor-school accessed knowledge_read school/power-electronics"). Sama wartość tokena nigdy nie trafia do logów.

**Po edycji `auth.yaml` zrestartuj kontener** — nie ma hot-reloadu:
```bash
cd /opt/mcp-brain && docker compose restart
```

## Generowanie tokena

```bash
printf 'tok_%s\n' "$(openssl rand -hex 32)"
```

Konwencja prefiksu `tok_` jest opcjonalna ale ułatwia rozpoznawanie sekretów w gistach/diffach.

## Scope grammar

Trzy-segmentowa składnia z wildcardami `*`:

```
<resource>:<action>:<scope>
```

Dla zasobów bez sensownego rozróżnienia read/write (inbox, briefing, secrets_schema) drugi segment jest pominięty:

```
<resource>:<scope>
```

| Tool                | Wymagane (lub filtruje po)                            |
|---------------------|-------------------------------------------------------|
| `knowledge_read`    | `knowledge:read:<scope>`                              |
| `knowledge_update`  | `knowledge:write:<scope>`                             |
| `knowledge_list`    | filtruje do tych `<scope>` które token może czytać    |
| `inbox_list`        | `inbox:read`                                          |
| `inbox_add`         | `inbox:write`                                         |
| `inbox_accept`      | `inbox:write` AND `knowledge:write:<target_scope>`    |
| `inbox_reject`      | `inbox:write`                                         |
| `get_briefing`      | filtruje sekcje do `briefing:<scope>` (lub `*`)       |
| `secrets_schema`    | filtruje wpisy do `secrets_schema:<scope>` (lub `*`)  |

### Wildcardy

| Granted              | Co matchuje                                      |
|----------------------|--------------------------------------------------|
| `*`                  | wszystko (god mode)                              |
| `knowledge:*:*`      | wszystkie operacje na knowledge, każdy scope     |
| `knowledge:read:*`   | tylko read na knowledge, każdy scope             |
| `knowledge:read:work`| tylko knowledge_read na `work/`                  |

Wildcardy działają tylko przy zachowanej liczbie segmentów. `knowledge:*` **nie matchuje** `knowledge:read:work` (różna arity). Jedyny wyjątek to bare `*`.

## Przykładowe role

### Pełny dostęp dla siebie

```yaml
- id: claude-code-laptop
  token: "tok_..."
  scopes: ["*"]
```

### Cursor / kolega — tylko studia

```yaml
- id: cursor-student
  token: "tok_..."
  scopes:
    - knowledge:read:school
    - knowledge:write:school
    - inbox:read
    - inbox:write
    - briefing:school
```

Może czytać/pisać tylko `school/`. `knowledge_list()` pokaże mu tylko pliki ze school. `get_briefing()` pokaże tylko sekcję school + preamble (timezone, preferencje). Nie zobaczy `secrets_schema` w ogóle.

### n8n homelab — read-only monitoring

```yaml
- id: n8n-homelab-ro
  token: "tok_..."
  scopes:
    - knowledge:read:homelab
    - briefing:homelab
    - secrets_schema:homelab
```

Może czytać dokumentację homelaba i widzieć schemat sekretów homelaba (gdzie leżą, nie wartości). Nie ma write nigdzie. Nie widzi schoolu ani worka.

### Custom GPT — tylko briefing

```yaml
- id: custom-gpt-briefer
  token: "tok_..."
  scopes:
    - briefing:work
    - briefing:school
```

Pokaże jedynie briefing do tych dwóch scopes. Wszystko inne → 403/permission denied.

### Bot Discord — tylko inbox

```yaml
- id: discord-inbox-bot
  token: "tok_..."
  scopes:
    - inbox:write
```

Może wrzucać propozycje do inboxa. Nie może ich akceptować, czytać, ani widzieć knowledge.

## Rotacja tokenów

1. Wygeneruj nowy: `printf 'tok_%s\n' "$(openssl rand -hex 32)"`
2. Edytuj `data/auth.yaml`, dopisz nowy wpis (nie kasuj starego od razu)
3. `docker compose restart`
4. Zaktualizuj klienta na nowy token, zweryfikuj że działa
5. Skasuj stary wpis, `restart` ponownie

Nie ma deny-listy — usunięcie tokena z YAML go unieważnia natychmiast po restarcie.

## Co tokeny widzą w logach

Logi serwera uvicorna pokażą:

- IP klienta
- Request line (`GET /sse`, `POST /messages/...`)
- Status kod (200, 401)

W przyszłości (poza MVP) mogę dodać structured logging tool-name + token.id na każde wywołanie. Na razie minimalnie.

## Bezpieczeństwo i hardening

- **Bind tylko na localhost**: `docker-compose.yml` ma `127.0.0.1:8400` — TLS terminuj w reverse proxy.
- **`auth.yaml` chmod 600** — installer to robi automatycznie.
- **Knowledge ma osobny git inside `data/knowledge`** — historia wszystkich zmian, możesz `git log` i odtworzyć stan sprzed tygodnia.
- **Brak HTTP `Server` headera obfuscation** — single-user, nie ma sensu.
- **Brak rate limitingu w MVP** — Caddy może to zrobić jeśli zechcesz (`@limit { ... }`).
- **`secrets_schema` nigdy nie zwraca wartości** — tylko nazwy kluczy + lokalizację (np. "1Password / vault: Work").
