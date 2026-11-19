#!/bin/sh
# Starts as root only to fix ownership of the data volume, then drops to PUID:PGID
# (Unraid default 99:100 = nobody:users). If the container is started with --user, it just runs.
set -e
if [ "$(id -u)" = "0" ]; then
  chown -R "${PUID}:${PGID}" /app/data 2>/dev/null || true
  exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups "$@"
fi
exec "$@"
