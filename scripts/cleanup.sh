#!/usr/bin/env bash
# Stop leftover sandbox containers. Volumes are untouched, so /workspace files
# survive. Wired to ExecStop= in mcp-sandbox.service: KillMode=mixed makes
# systemd SIGKILL conmon, which leaves the containers running unattached.
PODMAN="${SANDBOX_PODMAN:-/usr/bin/podman}"

names=$($PODMAN ps -a --filter name=mcpsb- --format '{{.Names}}' 2>/dev/null)
if [ -n "$names" ]; then
  # shellcheck disable=SC2086
  $PODMAN rm -f $names >/dev/null 2>&1
fi
exit 0
