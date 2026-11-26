#!/bin/sh
# Bring up the whole demo: Flagsmith, a prepared project, the sample app and
# Referee's dashboard. Needs Docker and Python 3.9+; installs nothing.
#
#   ./demo/up.sh          start (or restart) everything
#   ./demo/up.sh reset    delete the demo flag and its experiments, for another take
#   ./demo/up.sh down     stop and delete everything, including Flagsmith's data
set -eu
cd "$(dirname "$0")/.."

if [ "${1:-}" = "down" ]; then
  [ -f flagsmith.env ] || : > flagsmith.env
  docker compose --profile demo down --volumes --remove-orphans
  rm -f flagsmith.env
  echo "Stopped. Flagsmith's data and flagsmith.env are gone; .referee/ (plans and the decision ledger) is kept."
  exit 0
fi

PYTHON="${PYTHON:-python3}"
if [ "${1:-}" = "reset" ]; then
  exec "$PYTHON" demo/reset.py
fi
REFEREE_UID="$(id -u)"; REFEREE_GID="$(id -g)"; export REFEREE_UID REFEREE_GID
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "Python 3.9+ is required." >&2; exit 1; }

# Compose reads flagsmith.env for the demo services, so it has to exist.
[ -f flagsmith.env ] || : > flagsmith.env

echo "==> Starting Flagsmith (first run downloads the image; give it a minute)"
docker compose up -d postgres flagsmith flagsmith-task-processor

echo "==> Preparing a project, environment and keys"
"$PYTHON" scripts/bootstrap.py --url "http://localhost:${FLAGSMITH_PORT:-8000}" --out flagsmith.env

echo "==> Starting the sample app and Referee's dashboard"
mkdir -p .referee
docker compose --profile demo up -d --build app referee

cat <<EOF

Ready.
  Sample app          http://localhost:${APP_PORT:-8080}
  Referee dashboard   http://localhost:${REFEREE_PORT:-8787}
  Flagsmith           http://localhost:${FLAGSMITH_PORT:-8000}   (referee@example.com / Referee-local-2026!)

Next: ./demo/walkthrough.sh runs an experiment from plan to proposal,
or point your coding agent at Referee (see README, "Use it from a coding agent").
EOF
