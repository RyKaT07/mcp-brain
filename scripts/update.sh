#!/usr/bin/env bash
# Thin wrapper around install.sh's update path. Symlink as
# /usr/local/bin/mcp-brain-update for ergonomics.
set -euo pipefail
exec bash "$(dirname "$0")/install.sh" update "$@"
