#!/usr/bin/env bash
# Submit the section 4.1 reference case and print the recommendation, its
# citations and the run accounting. This is demonstration items 2, 3 and 10.
#
#   scripts/demo.sh [api-base-url]
set -euo pipefail

API="${1:-http://localhost:8080}"
PY="${PYTHON:-python}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REF="DEMO-$(date +%s)"

DESCRIPTION="I was making a food delivery run in my own car when the accident happened. A two-wheeler cut across at the junction, I braked hard and hit the kerb. The front bumper and bonnet are damaged and the left headlight is broken."

BODY="$("$PY" "${HERE}/_make_fnol.py" "$REF" "food_or_parcel_delivery" "$DESCRIPTION" "48000")"

echo "submitting the section 4.1 reference case to ${API}"
RUN_ID="$(curl -fsS -X POST "${API}/v1/claims" \
  -H 'Content-Type: application/json' \
  -H 'X-Client-Id: demo' \
  -d "$BODY" | "$PY" "${HERE}/_json_path.py" run_id)"

if [ -z "$RUN_ID" ]; then
  echo "submission did not return a run id" >&2
  exit 1
fi
echo "  run ${RUN_ID}"

i=0
while [ "$i" -lt 240 ]; do
  STATUS="$(curl -fsS "${API}/v1/runs/${RUN_ID}" | "$PY" "${HERE}/_json_path.py" status)"
  printf '\r  status: %-12s' "${STATUS}"
  case "$STATUS" in
    completed | escalated | failed | rejected) break ;;
  esac
  sleep 1
  i=$((i + 1))
done
echo

curl -fsS "${API}/v1/runs/${RUN_ID}/result" | "$PY" "${HERE}/_print_result.py"

echo "  full trace:  curl -s ${API}/v1/runs/${RUN_ID}/trace | ${PY} -m json.tool"
echo
