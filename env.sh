#!/usr/bin/env bash
# Create mcp-sandbox.env and generate its signing key. Edit the file, then run
# install.sh, which refuses to start the service with a placeholder
# MCP_PUBLIC_BASE. Never overwrites an existing env file.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${SANDBOX_HOME:-$HOME/mcp-linux-sandbox}"
ENVFILE="$DST/mcp-sandbox.env"

if [ -f "$ENVFILE" ]; then
  echo "$ENVFILE already exists; edit it directly"
  exit 0
fi

mkdir -p "$DST"
install -m 600 "$SRC/mcp-sandbox.env.example" "$ENVFILE"
if command -v openssl >/dev/null; then
  sed -i "s|^MCP_SIGN_KEY=.*|MCP_SIGN_KEY=$(openssl rand -hex 32)|" "$ENVFILE"
  echo "created $ENVFILE with a fresh MCP_SIGN_KEY"
else
  echo "created $ENVFILE; openssl not found, generate MCP_SIGN_KEY by hand"
fi

echo
echo "next:"
echo "  1. set MCP_PUBLIC_BASE in $ENVFILE to the URL clients reach this server on"
echo "  2. run: $SRC/install.sh"
