"""Split markdown documents into stable, content-hashed chunks.

Splitting strategy
------------------
1. Each H2 ("## ...") section becomes one chunk by default. The section
   title is part of the chunk text so the embedding picks up the heading
   as semantic context.
2. If a section is longer than ``MAX_TOKENS_APPROX`` (a rough
   words-as-tokens proxy), it is sub-split on H3 ("### ...") boundaries.
   If still too long, a sliding window of ``WINDOW_TOKENS`` words with
   ``OVERLAP_TOKENS`` overlap is used as the final fallback.
3. The text *before* the first H2 (the preamble) becomes its own chunk
   labelled ``_preamble`` so a file that uses no headings still gets
   indexed.

Stable IDs
----------
Each chunk has an ID derived from ``sha256(scope/project + heading_path
+ chunk_idx)``. The ID is stable across content edits — only the
``content_hash`` (sha256 of the chunk text) changes. The diff-aware
re-embed loop in ``service.py`` skips chunks where the new
``content_hash`` matches the stored one, so a typical edit re-embeds
1-2 chunks instead of the whole file.

This module has zero runtime dependencies — it's pure Python and safe
to import even when the ``embeddings`` extra is not installed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


# Approximate tokens-as-words. Real tokenisation depends on the model;
# bge-small uses WordPiece (≈ 1.3 tokens / word average). 320 words ≈
# 400 tokens — comfortable headroom under bge-small's 512 limit.
MAX_TOKENS_APPROX = 320
WINDOW_TOKENS = 280
OVERLAP_TOKENS = 50

_RE_H2 = re.compile(r"^## (?!#)(.+)$")
_RE_H3 = re.compile(r"^### (?!#)(.+)$")


@dataclass(frozen=True)
class Chunk:
    """One indexed slice of a markdown file.

    ``chunk_id`` is stable across edits; only ``content_hash`` changes
    when the user rewrites the section. ``heading_path`` is the
    breadcrumb for display ("## Section / ### Subsection").
    """

    chunk_id: str
    scope: str
    project: str
    heading_path: str
    chunk_idx: int
    text: str
    content_hash: str
    token_estimate: int


def _hash_id(scope: str, project: str, heading_path: str, chunk_idx: int) -> str:
    h = hashlib.sha256()
    h.update(f"{scope}/{project}\x00{heading_path}\x00{chunk_idx}".encode())
    return h.hexdigest()[:32]


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _tokens(text: str) -> int:
    """Cheap word-count proxy for token estimation."""
    return len(text.split())


def _slide(words: list[str]) -> list[str]:
    """Sliding window of ``words`` with overlap, returns chunk texts."""
    if not words:
        return []
    if len(words) <= WINDOW_TOKENS:
        return [" ".join(words)]
    out: list[str] = []
    step = WINDOW_TOKENS - OVERLAP_TOKENS
    i = 0
    while i < len(words):
        piece = words[i : i + WINDOW_TOKENS]
        if not piece:
            break
        out.append(" ".join(piece))
        if i + WINDOW_TOKENS >= len(words):
            break
        i += step
    return out


def _split_section(title: str, body: str) -> list[tuple[str, str]]:
    """Sub-split a long H2 section by H3, falling back to sliding window.

    Returns a list of ``(heading_path, text)`` tuples.
    """
    full = f"## {title}\n{body}".strip() if title else body.strip()
    if _tokens(full) <= MAX_TOKENS_APPROX:
        return [(title or "_preamble", full)]

    # Split on H3 if present.
    pieces: list[tuple[str, str]] = []
    current_h3: str | None = None
    current_lines: list[str] = []

    def flush():
        if not current_lines:
            return
        text = "\n".join(current_lines).strip()
        if not text:
            return
        path = f"{title} / {current_h3}" if current_h3 else title
        if not path:
            path = "_preamble"
        pieces.append((path, text))

    for line in body.splitlines():
        m = _RE_H3.match(line)
        if m:
            flush()
            current_h3 = m.group(1).strip()
            current_lines = [f"### {current_h3}"]
        else:
            current_lines.append(line)
    flush()

    if not pieces:
        # No H3s — fall through to sliding window over the whole section.
        return _slide_text(title or "_preamble", full)

    # Some sub-sections may still be too big — sliding-window each one.
    out: list[tuple[str, str]] = []
    for path, text in pieces:
        if _tokens(text) <= MAX_TOKENS_APPROX:
            out.append((path, text))
        else:
            out.extend(_slide_text(path, text))
    return out


def _slide_text(heading_path: str, text: str) -> list[tuple[str, str]]:
    pieces = _slide(text.split())
    return [(heading_path, p) for p in pieces]


def chunk_markdown(scope: str, project: str, content: str) -> list[Chunk]:
    """Split a markdown document into a list of ``Chunk``s."""
    if not content.strip():
        return []

    preamble_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_body: list[str] = []

    def flush_section():
        if current_title is not None:
            sections.append((current_title, current_body[:]))

    for line in content.splitlines():
        m = _RE_H2.match(line)
        if m:
            flush_section()
            current_title = m.group(1).strip()
            current_body = []
        else:
            if current_title is None:
                preamble_lines.append(line)
            else:
                current_body.append(line)
    flush_section()

    chunks: list[Chunk] = []
    chunk_idx = 0

    preamble = "\n".join(preamble_lines).strip()
    if preamble:
        for path, text in _split_section("", preamble):
            heading_path = path or "_preamble"
            chunks.append(
                Chunk(
                    chunk_id=_hash_id(scope, project, heading_path, chunk_idx),
                    scope=scope,
                    project=project,
                    heading_path=heading_path,
                    chunk_idx=chunk_idx,
                    text=text,
                    content_hash=_hash_content(text),
                    token_estimate=_tokens(text),
                )
            )
            chunk_idx += 1

    for title, body_lines in sections:
        body = "\n".join(body_lines).rstrip()
        for path, text in _split_section(title, body):
            chunks.append(
                Chunk(
                    chunk_id=_hash_id(scope, project, path, chunk_idx),
                    scope=scope,
                    project=project,
                    heading_path=path,
                    chunk_idx=chunk_idx,
                    text=text,
                    content_hash=_hash_content(text),
                    token_estimate=_tokens(text),
                )
            )
            chunk_idx += 1

    return chunks
