"""Vector / semantic-search subsystem.

Layout:
- ``chunker``  — split markdown files into stable, content-hashed chunks.
- ``embedder`` — fastembed wrapper (lazy model load, batch encode).
- ``store``    — sqlite-vec backed persistence and KNN query.
- ``service``  — orchestrator that ties it all together (refresh a single
  file, refresh a directory, query, garbage-collect).

Nothing in this package is imported eagerly by ``mcp_brain.server``;
optional deps (``fastembed``, ``sqlite-vec``) live behind an ``embeddings``
extra and the service degrades to a no-op if they are missing.
"""

from mcp_brain.embeddings.chunker import Chunk, chunk_markdown
from mcp_brain.embeddings.service import EmbeddingService

__all__ = ["Chunk", "chunk_markdown", "EmbeddingService"]
