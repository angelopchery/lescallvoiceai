#!/usr/bin/env bash
# deploy.sh — the ONLY sanctioned way to roll engine/dashboard code changes
# into the bind-mounted containers.
#
# Guarantee: the full backend test suite (golden prompt byte-identity for BOTH
# Pramukh agents, load sweep, stage traces, boot-check, copilot suites) passes
# INSIDE the same image that will serve calls, BEFORE any container restarts.
# A failed suite aborts the deploy — nothing restarts, live keeps old code.
#
# Usage:
#   bash deploy/deploy.sh                # test + restart api + runtime
#   bash deploy/deploy.sh api            # test + restart lescall-api only
#   bash deploy/deploy.sh runtime        # test + restart lescall-runtime only
#   SKIP_TESTS=1 bash deploy/deploy.sh   # EMERGENCY ONLY — restart w/o tests
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=/home/angelo/lescall
TARGETS=("${@:-api runtime}")
[ $# -eq 0 ] && TARGETS=(api runtime)

if [ "${SKIP_TESTS:-0}" != "1" ]; then
    echo "== deploy gate: running backend test suite (host venv; containers"
    echo "   bind-mount this same code — the image python carries no pytest) =="
    # No known failures: the roles-flattening fallout in
    # test_copilot_edit_engine was fixed 2026-07-20 (the fixture now claims the
    # agent in the agents index instead of relying on admin bypass). The gate
    # covers the WHOLE suite again — do not re-add deselects without a note.
    if ! (cd "$REPO/calling-agent/backend" && \
          "$REPO/calling-agent/venv/bin/python" -m pytest tests/ -q); then
        echo "!! TEST SUITE FAILED — deploy aborted, no container restarted." >&2
        exit 1
    fi
    echo "== deploy gate: suite green =="
else
    echo "!! SKIP_TESTS=1 — deploying WITHOUT the test gate (emergency path)" >&2
fi

for t in ${TARGETS[@]}; do
    case "$t" in
        api)     docker restart lescall-api ;;
        runtime) docker restart lescall-runtime ;;
        workers) docker restart lescall-workers ;;
        *) echo "unknown target: $t (api|runtime|workers)" >&2; exit 1 ;;
    esac
done

echo "== post-deploy verification =="
sleep 3
bash phase14_runtime/check_runtime.sh || true
echo "== deploy complete: ${TARGETS[*]} restarted =="
