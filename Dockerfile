FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY mcp_brain/ mcp_brain/

VOLUME /app/knowledge
EXPOSE 8400

CMD ["mcp-brain"]
