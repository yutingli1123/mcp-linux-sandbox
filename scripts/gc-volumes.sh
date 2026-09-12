#!/usr/bin/env bash
# Delete sandbox volumes that have not been touched for MAX_AGE_DAYS.
#
# Runs in dry-run mode by default. Leave DRY_RUN=1 for at least a week so you can
# confirm the age heuristic behaves on your filesystem before letting it delete.
#
# NEVER use `podman volume prune` instead of this: sandbox containers are
# recycled on purpose, so "not in use" is the normal state and prune would wipe
# every sandbox's files at once.
set -uo pipefail

PODMAN="${SANDBOX_PODMAN:-/usr/bin/podman}"
VOLROOT="${SANDBOX_VOLROOT:-$HOME/.local/share/containers/storage/volumes}"
NAME_PREFIX="mcpsb-vol-"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-7}"
DRY_RUN="${DRY_RUN:-1}"

now=$(date +%s)
shopt -s nullglob

found=0
for dir in "$VOLROOT/${NAME_PREFIX}"*; do
  found=1
  name=$(basename "$dir")

  if [ -n "$($PODMAN ps -a --filter "volume=${name}" \
        --format '{{.Names}}' 2>/dev/null | head -1)" ]; then
    echo "keep   (in use)     ${name}"
    continue
  fi

  # Last activity = newest mtime among the volume dir, _data, and files in _data.
  latest=0
  for f in "$dir" "$dir/_data"; do
    [ -e "$f" ] || continue
    t=$(stat -c %Y "$f" 2>/dev/null || echo 0)
    [ "$t" -gt "$latest" ] && latest=$t
  done
  t=$(find "$dir/_data" -mindepth 1 -printf '%T@\n' 2>/dev/null \
        | sort -n | tail -1 | cut -d. -f1)
  [ -n "${t:-}" ] && [ "$t" -gt "$latest" ] && latest=$t

  # Fail closed: an unreadable volume would otherwise look infinitely old.
  if [ "$latest" -eq 0 ]; then
    echo "keep   (age unknown) ${name}"
    continue
  fi

  age=$(( (now - latest) / 86400 ))

  if [ "$age" -ge "$MAX_AGE_DAYS" ]; then
    if [ "$DRY_RUN" = "0" ]; then
      echo "DELETE (age ${age}d)  ${name}"
      $PODMAN volume rm "$name"
    else
      echo "WOULD  (age ${age}d)  ${name}"
    fi
  else
    echo "keep   (age ${age}d)  ${name}"
  fi
done

[ "$found" = 0 ] && echo "(no sandbox volumes found)"
exit 0
