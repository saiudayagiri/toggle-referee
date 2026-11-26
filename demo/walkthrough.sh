#!/bin/sh
# One experiment, start to proposal, on the demo stack from ./demo/up.sh.
# Every step is a plain `referee` command you could type yourself.
#
#   ./demo/walkthrough.sh            run it
#   VISITORS=4000 ./demo/walkthrough.sh
set -eu
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
referee() { "$PYTHON" -m referee "$@"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

[ -f flagsmith.env ] || { echo "Run ./demo/up.sh first." >&2; exit 1; }

step "1. Is Flagsmith reachable?"
referee doctor

step "2. The plan: what is tested, how success is measured, how long it needs"
referee plan demo/plan.json

step "3. Create the flag, keyed variant, metrics and experiment in Flagsmith (nothing starts)"
referee apply demo/plan.json || {
  echo
  echo "If this flag already finished an experiment, run ./demo/up.sh reset and try again." >&2
  exit 1
}

step "4. Preflight: is Flagsmith set up, and does the app's code record exposures and send the events?"
referee preflight signup_form --code demo/app

step "5. Start (refused if preflight fails)"
referee start signup_form --code demo/app | tail -n 2

step "6. Does Flagsmith hand out both versions? (200 made-up identities, no exposures recorded)"
referee preflight signup_form --probe 200 | grep -A1 "assigns\|Assignment\|never served" || true

step "7. Visitors arrive (simulated: each extra form field loses some of them)"
"$PYTHON" demo/simulate.py --visitors "${VISITORS:-2400}" --url "http://localhost:${APP_PORT:-8080}"
sleep 8   # the SDK sends events in batches

step "8. The scorecard: Flagsmith's numbers, Referee's checks, and the call"
referee scorecard signup_form

step "9. Propose the rollout"
if referee propose signup_form --by walkthrough; then
  cat <<EOF

Nothing has changed in Flagsmith yet. Approve it yourself, as a person:
  dashboard   http://localhost:${REFEREE_PORT:-8787}   (sign in with the Flagsmith account from ./demo/up.sh)
  terminal    $PYTHON -m referee approve <decision id>
Then:         $PYTHON -m referee cleanup signup_form --code demo/app
EOF
else
  echo "No rollout to propose yet; the call above says why. Send more visitors and run 'referee scorecard signup_form' again."
fi
