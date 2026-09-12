#!/usr/bin/env bash
# Idempotent installer / updater for the MCP Linux sandbox.
# Run as the target user (rootless podman); it only touches the user's own
# systemd units plus the image build.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${SANDBOX_HOME:-$HOME/mcp-linux-sandbox}"
UNITDIR="$HOME/.config/systemd/user"
PODMAN="${SANDBOX_PODMAN:-/usr/bin/podman}"

say() { printf '\n== %s\n' "$*"; }

say "system prerequisites"
missing=()
command -v "$PODMAN" >/dev/null || missing+=("podman")
rpm -q catatonit >/dev/null 2>&1 || missing+=("catatonit (needed by podman --init)")
if [ ${#missing[@]} -gt 0 ]; then
  echo "missing: ${missing[*]}"
  echo "on Fedora:  sudo dnf install -y podman catatonit"
  exit 1
fi
case "$($PODMAN info --format '{{.Host.CgroupsVersion}}' 2>/dev/null)" in
  v2) : ;;
  *) echo "warning: cgroups v2 not detected; --memory/--cpus may be ignored" ;;
esac

say "copying files to $DST"
if [ "$SRC" != "$DST" ]; then
  mkdir -p "$DST"
  install -m 755 "$SRC/server.py" "$DST/server.py"
  install -m 644 "$SRC/Dockerfile" "$DST/Dockerfile"
  install -m 644 "$SRC/requirements.txt" "$DST/requirements.txt"
  install -m 755 "$SRC/env.sh" "$DST/env.sh"
  install -m 755 "$SRC/selftest.py" "$DST/selftest.py"
  mkdir -p "$DST/scripts" "$DST/systemd" "$DST/nginx"
  install -m 755 "$SRC"/scripts/*.sh "$DST/scripts/"
  install -m 644 "$SRC"/systemd/* "$DST/systemd/"
  install -m 644 "$SRC"/nginx/* "$DST/nginx/"
  [ -f "$SRC/mcp-sandbox.env.example" ] &&
    install -m 644 "$SRC/mcp-sandbox.env.example" "$DST/"
fi

say "environment file"
if [ ! -f "$DST/mcp-sandbox.env" ]; then
  echo "missing $DST/mcp-sandbox.env"
  echo "run: $SRC/env.sh   (creates it and generates the signing key)"
  exit 1
fi
if grep -q 'your\.host\.example' "$DST/mcp-sandbox.env"; then
  echo "MCP_PUBLIC_BASE in $DST/mcp-sandbox.env is still the placeholder"
  echo "set it to the URL clients reach this server on, then re-run."
  exit 1
fi
echo "using $DST/mcp-sandbox.env"

say "python venv"
[ -d "$DST/venv" ] || python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" install --quiet --upgrade pip
"$DST/venv/bin/pip" install --quiet -r "$DST/requirements.txt"
"$DST/venv/bin/python" -c "import fastmcp; print('fastmcp', getattr(fastmcp,'__version__','?'))"

say "base image"
$PODMAN image exists "$($PODMAN images --format '{{.Repository}}:{{.Tag}}' \
  | grep -m1 sandbox-base:latest || echo localhost/sandbox-base:latest)" \
  && echo "image already present (rebuild with: $PODMAN build -t localhost/sandbox-base:latest $DST)" \
  || $PODMAN build -t localhost/sandbox-base:latest "$DST"

say "systemd user units"
mkdir -p "$UNITDIR"
install -m 644 "$DST/systemd/mcp-sandbox.service" "$UNITDIR/"
install -m 644 "$DST/systemd/mcp-volumes-gc.service" "$UNITDIR/"
install -m 644 "$DST/systemd/mcp-volumes-gc.timer" "$UNITDIR/"

# The units hardcode %h/mcp-linux-sandbox; rewrite if SANDBOX_HOME differs.
if [ "$DST" != "$HOME/mcp-linux-sandbox" ]; then
  sed -i "s|%h/mcp-linux-sandbox|$DST|g" "$UNITDIR"/mcp-sandbox.service \
    "$UNITDIR"/mcp-volumes-gc.service
  echo "rewrote unit paths for $DST"
fi

# Without linger the user manager exits at logout and stops the service with it.
loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable mcp-sandbox
systemctl --user enable --now mcp-volumes-gc.timer
# restart, not `enable --now`: starting an already-active unit is a no-op, so a
# re-install would keep running the old environment file.
systemctl --user restart mcp-sandbox

say "status"
systemctl --user --no-pager --lines=0 status mcp-sandbox || true
echo
echo "next:"
echo "  1. point nginx at 127.0.0.1:$(grep -oP 'SANDBOX_PORT=\K.*' "$DST/mcp-sandbox.env" || echo 8000)"
echo "     (see $DST/nginx/mcp-sandbox.conf)"
echo "  2. configure the MCP client with the URL and bearer token"
echo "  3. run: $DST/venv/bin/python $DST/selftest.py --help"
