"""
Localized message catalog for tool output.

Tool responses are addressed to the calling LLM, which then surfaces
them to the user. We localize so the user (or their reading client)
sees responses in their preferred language.

Resolution order at startup:

1. `MCP_LANG` env (e.g. `pl`, `en`) — explicit override
2. `user.lang` in `meta.yaml`
3. fallback: `en`

There is no per-request locale negotiation. Single-user server, one
language per running process. Restart to change.

Tools must use `t("key", **fmt)` instead of literal strings for any
message that ends up in the user-facing return value. New keys go in
both catalogs at once — `_check_catalog_parity()` enforces this on
import so a missing translation fails loudly during dev.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import yaml

SUPPORTED: Final[tuple[str, ...]] = ("en", "pl")
DEFAULT: Final[str] = "en"


_EN: dict[str, str] = {
    # generic
    "permission_denied": "Permission denied: {scope}",
    # knowledge
    "no_knowledge_file": "No knowledge file found: {scope}/{project}",
    "section_not_found": "Section '{section}' not found. Available: {available}",
    "knowledge_updated": "Updated {scope}/{project} § {section}",
    "no_knowledge_files": "No knowledge files found.",
    # inbox
    "inbox_empty": "Inbox is empty.",
    "inbox_added": "Added to inbox: [{item_id}] {summary}",
    "inbox_item_not_found": "Item {item_id} not found.",
    "inbox_no_target": (
        "Item {item_id} has no suggested_target. Update it first or merge manually."
    ),
    "inbox_invalid_target": (
        "Invalid target format: {target}. Expected 'scope/project'."
    ),
    "inbox_accepted": "Accepted [{item_id}] → {scope}/{project} § {section}",
    "inbox_rejected": "Rejected [{item_id}]",
    # briefing
    "briefing_no_meta": "No meta.yaml found. Create one in the knowledge directory.",
    "briefing_title": "Briefing for {name}",
    "briefing_timezone": "Timezone: {tz}",
    "briefing_preferences_header": "Preferences",
    "briefing_files_for_scope": "Available knowledge files ({scope}/)",
    # secrets_schema
    "secrets_no_meta": "No meta.yaml found.",
    "secrets_no_schema": "No secrets_schema defined in meta.yaml.",
    "secrets_no_scope": "No secrets schema for scope '{scope}'. Available: {available}",
    "secrets_none_visible": (
        "Permission denied: no secrets_schema scopes available to this token."
    ),
    "secrets_location_label": "Location",
    "secrets_keys_label": "Keys",
}


_PL: dict[str, str] = {
    # generic
    "permission_denied": "Brak uprawnień: {scope}",
    # knowledge
    "no_knowledge_file": "Nie znaleziono pliku wiedzy: {scope}/{project}",
    "section_not_found": "Sekcja '{section}' nie istnieje. Dostępne: {available}",
    "knowledge_updated": "Zaktualizowano {scope}/{project} § {section}",
    "no_knowledge_files": "Brak plików wiedzy.",
    # inbox
    "inbox_empty": "Inbox jest pusty.",
    "inbox_added": "Dodano do inboxa: [{item_id}] {summary}",
    "inbox_item_not_found": "Element {item_id} nie znaleziony.",
    "inbox_no_target": (
        "Element {item_id} nie ma suggested_target. Uzupełnij go lub zmerguj ręcznie."
    ),
    "inbox_invalid_target": (
        "Nieprawidłowy format target: {target}. Oczekiwano 'scope/project'."
    ),
    "inbox_accepted": "Zaakceptowano [{item_id}] → {scope}/{project} § {section}",
    "inbox_rejected": "Odrzucono [{item_id}]",
    # briefing
    "briefing_no_meta": "Nie znaleziono meta.yaml. Utwórz go w katalogu knowledge.",
    "briefing_title": "Briefing dla {name}",
    "briefing_timezone": "Strefa czasowa: {tz}",
    "briefing_preferences_header": "Preferencje",
    "briefing_files_for_scope": "Dostępne pliki wiedzy ({scope}/)",
    # secrets_schema
    "secrets_no_meta": "Nie znaleziono meta.yaml.",
    "secrets_no_schema": "Brak secrets_schema w meta.yaml.",
    "secrets_no_scope": "Brak schematu sekretów dla zakresu '{scope}'. Dostępne: {available}",
    "secrets_none_visible": (
        "Brak uprawnień: ten token nie ma dostępu do żadnego scope w secrets_schema."
    ),
    "secrets_location_label": "Lokalizacja",
    "secrets_keys_label": "Klucze",
}


_CATALOGS: dict[str, dict[str, str]] = {"en": _EN, "pl": _PL}


def _check_catalog_parity() -> None:
    """Fail loudly at import time if any translation key is missing."""
    en_keys = set(_EN)
    for lang, catalog in _CATALOGS.items():
        if lang == "en":
            continue
        missing = en_keys - catalog.keys()
        extra = catalog.keys() - en_keys
        if missing or extra:
            raise RuntimeError(
                f"i18n catalog mismatch for {lang!r}: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )


_check_catalog_parity()


_lang: str = DEFAULT


def _detect_lang(knowledge_dir: Path) -> str:
    env = os.getenv("MCP_LANG", "").strip().lower()
    if env in SUPPORTED:
        return env

    meta = knowledge_dir / "meta.yaml"
    if meta.exists():
        try:
            data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
        except Exception:
            return DEFAULT
        user = data.get("user") or {}
        lang = str(user.get("lang", "")).strip().lower()
        if lang in SUPPORTED:
            return lang

    return DEFAULT


def init(knowledge_dir: Path) -> None:
    """Resolve and lock the runtime language. Call once at startup."""
    global _lang
    _lang = _detect_lang(knowledge_dir)


def current_lang() -> str:
    return _lang


def t(key: str, **fmt: object) -> str:
    """Look up `key` in the active catalog, fall back to English on miss."""
    catalog = _CATALOGS.get(_lang, _EN)
    template = catalog.get(key) or _EN[key]
    return template.format(**fmt)
