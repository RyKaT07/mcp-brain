# syntax=docker/dockerfile:1.6
#
# mcp-brain — multi-stage build
#
# Stage 1: build a wheel from the source tree.
# Stage 2: minimal runtime with git (needed for knowledge auto-commit) and
#          a non-root user. Mounts /data for knowledge + auth config.

FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY mcp_brain/ ./mcp_brain/
RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim AS runtime

# git: knowledge auto-commit. curl: HEALTHCHECK probe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — uid 1000 lines up with the most common host uid so
# bind-mounted knowledge/ stays writable from outside the container.
RUN groupadd --gid 1000 mcpbrain \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash mcpbrain

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
    && pip install --no-cache-dir pdfplumber python-docx \
    && rm -f /tmp/*.whl

# Default data layout. Knowledge files and auth.yaml live under /data,
# which the host bind-mounts.
ENV MCP_TRANSPORT=sse \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8400 \
    MCP_KNOWLEDGE_DIR=/data/knowledge \
    MCP_AUTH_CONFIG=/data/auth.yaml

# Make sure git's safe.directory check doesn't trip on bind-mounted dirs
# owned by a different uid. Single-user box, low-risk.
RUN git config --system --add safe.directory '*'

USER mcpbrain
WORKDIR /home/mcpbrain
VOLUME /data
EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${MCP_PORT}/healthz || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["mcp-brain"]
