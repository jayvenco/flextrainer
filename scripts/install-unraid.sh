#!/usr/bin/env bash
# Install or update FlexTrainer on Unraid (manageable from the Docker tab).
#
# Pulls the image from GHCR, creates the container with Unraid labels (icon, WebUI button)
# and writes a user template so "Edit" in the Docker tab works.
# Running it again = update; your data in <appdata>/data stays.
#
# Override with environment variables, e.g.:  HOST_PORT=9000 bash install-unraid.sh
set -euo pipefail

NAME="${CONTAINER_NAME:-flextrainer}"
IMAGE="${IMAGE:-ghcr.io/jayvenco/flextrainer}"
TAG="${IMAGE_TAG:-latest}"
HOST_PORT="${HOST_PORT:-8822}"
CONTAINER_PORT=8000
APPDATA="${APPDATA:-/mnt/user/appdata/${NAME}}"
RAW="${RAW_URL:-https://raw.githubusercontent.com/jayvenco/flextrainer/main/unraid}"
TEMPLATE_DIR="${TEMPLATE_DIR:-/boot/config/plugins/dockerMan/templates-user}"
# Optional app settings. If they are already set on the existing container (e.g. via the Docker tab)
# they are kept on update; a value you pass here wins.
OPTIONAL_VARS="TZ PUID PGID OPENAI_API_KEY OPENAI_MODEL OPENAI_BASE_URL BACKUP_ENABLED BACKUP_KEEP DEFAULT_THEME BASE_URL ALLOW_PRIVATE_SCRAPE"

log() { printf '==> %s\n' "$*"; }

command -v docker >/dev/null 2>&1 || { echo "Docker not found. Run this script on the Unraid server." >&2; exit 1; }

# 1. Appdata folders (Unraid default owner nobody:users = 99:100)
log "Creating folders in ${APPDATA}"
mkdir -p "${APPDATA}/data"
chown -R 99:100 "${APPDATA}" 2>/dev/null || true

# 2. Optional GHCR login (only needed if the package is private)
if [ -n "${GHCR_USER:-}" ] && [ -n "${GHCR_TOKEN:-}" ]; then
  log "Logging in to ghcr.io as ${GHCR_USER}"
  printf '%s' "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
fi

# 3. Pull the image
if [ "${SKIP_PULL:-0}" = "1" ]; then
  log "Pull skipped (SKIP_PULL=1), using local image ${IMAGE}:${TAG}"
else
  log "Pulling image: ${IMAGE}:${TAG}"
  docker pull "${IMAGE}:${TAG}"
fi

# 4. Replace the existing container (data lives on the volume and is kept)
if docker inspect "${NAME}" >/dev/null 2>&1; then
  OLD_ENV="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${NAME}" 2>/dev/null || true)"
  for v in ${OPTIONAL_VARS}; do
    if [ -z "${!v:-}" ]; then
      old="$(printf '%s\n' "${OLD_ENV}" | sed -n "s/^${v}=//p" | head -n 1)"
      [ -n "${old}" ] && printf -v "${v}" '%s' "${old}" && log "Kept existing setting ${v}"
    fi
  done
  log "Stopping and removing existing container '${NAME}' (data is kept)"
  docker stop "${NAME}" >/dev/null 2>&1 || true
  docker rm "${NAME}" >/dev/null
fi

ENV_ARGS=()
for v in ${OPTIONAL_VARS}; do
  [ -n "${!v:-}" ] && ENV_ARGS+=(-e "${v}=${!v}")
done

# 5. Start the container with labels so Unraid shows it as its own container
log "Starting container '${NAME}' on port ${HOST_PORT}"
docker run -d \
  --name "${NAME}" \
  --restart unless-stopped \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  -v "${APPDATA}/data:/app/data" \
  ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
  --label "net.unraid.docker.managed=dockerman" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:${CONTAINER_PORT}]/" \
  --label "net.unraid.docker.icon=${RAW}/icon.png" \
  "${IMAGE}:${TAG}" >/dev/null

# 6. Write the user template so Edit in the Docker tab shows the right values
if [ -d "${TEMPLATE_DIR}" ]; then
  log "Saving template to ${TEMPLATE_DIR}/my-${NAME}.xml"
  TMP="$(mktemp)"
  if command -v curl >/dev/null 2>&1 && curl -fsSL "${RAW}/flextrainer.xml" -o "${TMP}"; then
    # Make values safe for sed (escape & \ | /) and XML (& < >)
    esc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/[\\&|]/\\&/g'; }
    setcfg() { sed -i -E "s|(Target=\"$1\"[^>]*>)[^<]*(</Config>)|\1$(esc "$2")\2|" "${TMP}"; }
    sed -i -E "s|<Name>[^<]*</Name>|<Name>$(esc "${NAME}")</Name>|" "${TMP}"
    setcfg "8000" "${HOST_PORT}"
    setcfg "/app/data" "${APPDATA}/data"
    for v in ${OPTIONAL_VARS}; do
      [ -n "${!v:-}" ] && setcfg "${v}" "${!v}"
    done
    install -m 600 "${TMP}" "${TEMPLATE_DIR}/my-${NAME}.xml"
  else
    echo "Warning: could not download the template; skipping (the container still works)." >&2
  fi
  rm -f "${TMP}"
else
  log "No dockerMan folder found (not Unraid?); template skipped"
fi

# 7. Wait for the healthcheck to turn green
log "Waiting for the app to become healthy..."
STATUS="starting"
for _ in $(seq 1 60); do
  STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${NAME}" 2>/dev/null || echo missing)"
  case "${STATUS}" in healthy|running) break ;; unhealthy|exited|dead|missing) break ;; esac
  sleep 2
done
if [ "${STATUS}" != "healthy" ] && [ "${STATUS}" != "running" ]; then
  echo "Container is not healthy (status: ${STATUS}). Last log lines:" >&2
  docker logs --tail 30 "${NAME}" >&2 || true
  exit 1
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
log "Done! Open http://${IP:-<unraid-ip>}:${HOST_PORT}/"
echo "    First login: admin / admin (you must change it right away)."
echo "    You can now also manage it from the Docker tab (Edit, logs, autostart, updates)."
