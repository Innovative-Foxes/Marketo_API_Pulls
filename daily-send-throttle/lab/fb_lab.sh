#!/usr/bin/env bash
#
# FlowBoost lab harness - test the webhook payloads WITHOUT touching Marketo.
#
# This is the fastest possible loop for proving the counter logic: no leads, no
# campaigns, no waiting on batch runs. Run this until it behaves, and only then
# wire it into Marketo.
#
# Usage:
#   ./fb_lab.sh                       # one call to fb-daily-draw.js
#   ./fb_lab.sh -n 51                 # 51 calls - proves slots 1..51 (cap boundary)
#   ./fb_lab.sh -n 3 -f ../flowboost/fb-assign-sequence.js
#   ./fb_lab.sh -n 3 -p              # -p = PRODUCTION counter (no isolation!)
#
# By default every run rewrites COUNTER_PREFIX to a unique LAB namespace, so
# each run starts from a virgin counter and nothing you do here can pollute the
# real production count. Pass -p only when you deliberately want to inspect or
# advance the live counter.

set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "ERROR: no lab/.env - copy .env.example and add your key." >&2; exit 2; }

# Parse .env literally rather than `source`-ing it: the FlowBoost URL contains a
# literal "$current", which the shell would otherwise expand as a variable.
while IFS='=' read -r key value; do
  [[ $key =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue          # skip comments / blanks
  value="${value%\"}"; value="${value#\"}"               # tolerate "quoted"
  value="${value%\'}"; value="${value#\'}"               # tolerate 'quoted'
  printf -v "$key" '%s' "$value"
done < .env
: "${FLOWBOOST_API_KEY:?set FLOWBOOST_API_KEY in lab/.env}"
: "${FLOWBOOST_URL:?set FLOWBOOST_URL in lab/.env}"

COUNT=1
SCRIPT="../flowboost/fb-daily-draw.js"
ISOLATE=1

while getopts "n:f:p" opt; do
  case $opt in
    n) COUNT=$OPTARG ;;
    f) SCRIPT=$OPTARG ;;
    p) ISOLATE=0 ;;
    *) echo "usage: $0 [-n calls] [-f script.js] [-p]" >&2; exit 2 ;;
  esac
done

[[ -f $SCRIPT ]] || { echo "ERROR: no such script: $SCRIPT" >&2; exit 2; }

PAYLOAD=$(mktemp); trap 'rm -f "$PAYLOAD"' EXIT

if (( ISOLATE )); then
  RUN_ID="lab-$(date +%Y%m%d-%H%M%S)-$$"
  # Rewrite both possible namespace constants to an isolated LAB path. The first
  # pattern is fb-daily-draw.js's COUNTER_PREFIX line; the second is the indented
  # string literal in fb-assign-sequence.js. Both work whatever slug you chose.
  sed -E \
    -e "s|^const COUNTER_PREFIX = \".*\";|const COUNTER_PREFIX = \"/lab/LAB/${RUN_ID}\";|" \
    -e "s|^  \"/[^\"]*\";|  \"/lab/LAB/${RUN_ID}/seq\";|" \
    "$SCRIPT" > "$PAYLOAD"
  echo "namespace : LAB/${RUN_ID}  (isolated - production counter untouched)"
  if ! grep -q "LAB/${RUN_ID}" "$PAYLOAD"; then
    echo "ERROR: could not isolate the counter namespace in $SCRIPT." >&2
    echo "       Refusing to run against the production counter. Use -p to force." >&2
    exit 3
  fi
else
  cp "$SCRIPT" "$PAYLOAD"
  echo "namespace : PRODUCTION  (-p given; this WILL advance the live counter)"
fi

echo "script    : $SCRIPT"
echo "calls     : $COUNT"
echo

for ((i = 1; i <= COUNT; i++)); do
  resp=$(curl -sS -X POST "$FLOWBOOST_URL" \
    -H "Content-Type: application/javascript" \
    -H "X-Api-Key: ${FLOWBOOST_API_KEY}" \
    --data-binary "@${PAYLOAD}")
  printf 'call %3d  ->  %s\n' "$i" "$resp"
  case $resp in
    *ERR*|*error*|*Error*)
      echo
      echo "FlowBoost returned an error. Common causes:" >&2
      echo "  - 'Illegal return statement' / FBCounter undefined -> authoringEnv=pro missing from the URL" >&2
      echo "  - auth error -> wrong or missing X-Api-Key" >&2
      echo "  - 'ttl' rejected by autoAdd -> see the fallback in README.md step 2.4" >&2
      exit 1 ;;
  esac
done
