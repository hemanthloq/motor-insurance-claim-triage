#!/usr/bin/env bash
# Run the whole system in one process, with no Docker and no make.
#
#   scripts/run-local.sh            build the index if needed, then serve
#   scripts/run-local.sh --reindex  force a rebuild of the index first
#   scripts/run-local.sh --port 9000
#
# This is the development path. It uses the in-memory state backend, a local
# numpy vector store and the in-process stub provider, so it needs no Redis, no
# Chroma and no API key. Everything the service does is exercised -- the
# contracts, the boundary, the guardrails, the agent graph, the verifier, the
# trace -- except that the "model" cannot read a policy clause, so the coverage
# reasoning is placeholder text. See docs/stub-provider.md.
#
# To use a real model, set these three before running and the same path works:
#   export LLM_PROVIDER=openai_compat
#   export LLM_BASE_URL=https://api.groq.com/openai/v1
#   export LLM_API_KEY=...
#   export LLM_MODEL=llama-3.3-70b-versatile
set -euo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"

PORT=8080
REINDEX=0
while [ $# -gt 0 ]; do
  case "$1" in
    --reindex) REINDEX=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# ---- find the interpreter -------------------------------------------------
if   [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe"   # Windows
elif [ -x ".venv/bin/python" ];        then PY=".venv/bin/python"            # Linux / macOS
else
  echo "No virtualenv found. Create one first:"
  echo
  echo "    python -m venv .venv"
  echo "    .venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows"
  echo "    .venv/bin/python -m pip install -r requirements-dev.txt           # Linux/macOS"
  exit 1
fi

# ---- environment ----------------------------------------------------------
set -a
# shellcheck disable=SC1091
. scripts/dev.env
set +a
export PYTHONPATH=. PYTHONUTF8=1

# ---- corpus ---------------------------------------------------------------
if [ ! -f "corpus/raw/il_private_car_package_wording.pdf" ]; then
  echo "==> downloading the insurer's published documents"
  "$PY" -m claimtriage.cli corpus-fetch
fi

if [ "$REINDEX" = "1" ] || [ ! -f "data/index/vectors.npy" ]; then
  echo "==> building the clause index"
  "$PY" -m claimtriage.cli ingest
else
  echo "==> index already built (scripts/run-local.sh --reindex to rebuild)"
fi

# ---- serve ----------------------------------------------------------------
cat <<BANNER

  ------------------------------------------------------------------
    web client   http://localhost:${PORT}/
    API docs     http://localhost:${PORT}/docs
    health       http://localhost:${PORT}/health/ready
    corpus       http://localhost:${PORT}/health/corpus

    try it:      scripts/demo.sh http://localhost:${PORT}
    check all:   scripts/smoke.sh http://localhost:${PORT}

    Ctrl-C to stop.
  ------------------------------------------------------------------

BANNER

exec "$PY" -m uvicorn claimtriage.api.app:app --host 127.0.0.1 --port "$PORT"
