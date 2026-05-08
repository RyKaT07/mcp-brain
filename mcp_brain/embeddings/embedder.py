"""Thin wrapper around fastembed for batch sentence-embedding.

The model is loaded lazily on the first call to ``encode()`` so server
startup stays fast (and so unit tests that don't touch embeddings don't
need the optional dep). Subsequent calls reuse the loaded model.

Default model is ``BAAI/bge-small-en-v1.5`` (384 dim) — small, MIT
license, ~30 MB on disk, ~50 MB RSS, runs comfortably on a single LXC
vCPU (~50 ms / chunk batched). Override with ``MCP_EMBED_MODEL`` if a
different fastembed-supported model is wanted (e.g. ``BAAI/bge-m3`` for
multilingual).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384

# Model name → embedding dimension. Keep in sync with fastembed's catalogue.
KNOWN_DIMS: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "BAAI/bge-m3": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "intfloat/multilingual-e5-small": 384,
    "intfloat/multilingual-e5-base": 768,
}


class Embedder:
    """Lazy fastembed wrapper. Thread-safe singleton-style model load."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or os.getenv("MCP_EMBED_MODEL", DEFAULT_MODEL)
        self.dim = KNOWN_DIMS.get(self.model_name, DEFAULT_DIM)
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        """Load the model on first use. Raises if fastembed is missing."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                # Imported lazily so the module stays importable when the
                # ``embeddings`` extra is not installed (e.g. in unit tests
                # that exercise the chunker but don't load a model).
                from fastembed import TextEmbedding  # type: ignore[import-not-found]
            except ImportError as e:
                raise RuntimeError(
                    "fastembed is not installed. Install the 'embeddings' extra: "
                    "`pip install -e .[embeddings]`."
                ) from e
            logger.info("loading fastembed model: %s", self.model_name)
            # Cap ONNX runtime threads. Inside an LXC the kernel reports
            # the host's full CPU count via /proc/cpuinfo, so onnxruntime
            # spawns ~one thread per host core (often 128+) and immediately
            # tries pthread_setaffinity_np against the container's cgroup
            # cpuset, which fails with EINVAL for every thread that lands
            # outside the allowed mask. Each failure logs an error line,
            # the spawn storm starves the request loop, and panel calls
            # to knowledge_graph stall indefinitely.
            #
            # ``threads`` keyword on TextEmbedding maps to
            # SessionOptions.intra_op_num_threads. ``MCP_EMBED_THREADS``
            # is the override hook for tuning per deployment.
            try:
                threads = int(os.getenv("MCP_EMBED_THREADS", "2"))
            except ValueError:
                threads = 2
            try:
                self._model = TextEmbedding(
                    model_name=self.model_name,
                    threads=threads,
                )
            except TypeError:
                # Older fastembed versions don't accept the threads kwarg.
                # Fall back to the default constructor and cap via env.
                os.environ.setdefault("OMP_NUM_THREADS", str(threads))
                self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def encode(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed an iterable of texts; returns a list of float vectors.

        Vectors are returned as plain Python lists so callers don't need
        numpy. If you need numpy arrays for downstream linear-algebra,
        wrap with ``np.asarray()`` at the call site.
        """
        items = list(texts)
        if not items:
            return []
        model = self._ensure_model()
        # fastembed yields numpy ndarrays.
        return [list(map(float, vec)) for vec in model.embed(items)]

    def encode_one(self, text: str) -> list[float]:
        out = self.encode([text])
        return out[0] if out else []
