#!/usr/bin/env bash
# Container entrypoint for the background workers, one loop, staggered cadence:
#   every  5 min — usage-metering reconcile (billing referee; CDR join)
#   every  5 min — outbox delivery (client-ERP lead hand-off)
#   every 15 min — recording sync (GCS upload catch-up + local retention)
#   every 60 min — log shipper (rotated/idle logs -> gs://lescall-logs)
set -uo pipefail
cd /home/angelo/lescall

# Intelligence worker: a separate long-lived process, NOT a pass in the tick
# loop below — LLM jobs can take minutes and must never delay the billing
# reconcile (sentinel restarts this container if 'reconcile:' vanishes from
# 20 min of logs). Supervised: restarts 30s after any exit.
(
    while true; do
        python -m shared.intelligence.worker || true
        echo "WARN intel worker exited; restarting in 30s"
        sleep 30
    done
) &

# Billing sync + dunning: ALSO its own supervised process, for the same reason
# as the intel worker. It makes N sequential Razorpay calls per pass, so
# putting it in the tick loop below would make tick duration a function of the
# payment provider's latency — and the sentinel restarts this container when
# 'reconcile:' vanishes for 20 minutes. One slow afternoon at Razorpay would
# then kill the intel worker mid-job and stop billing reconciliation.
# Self-disables when the billing tables are absent (SQLite dev).
(
    while true; do
        python -m shared.billing.sync || true
        echo "WARN billing sync exited; restarting in 60s"
        sleep 60
    done
) &

TICK=300
i=0
echo "workers loop up (tick=${TICK}s)"
while true; do
    python -m shared.usage_metering --once           || echo "WARN metering pass failed"
    # Forwarded calls (RUNBOOK §84): CDR → forwarded_calls. These calls never
    # reach the AI, so this is their only record (and their minutes, counted
    # separately from AI minutes). A no-op until migration 0032 is applied.
    python -m shared.call_forwarding --sync          || echo "WARN forwarding sync failed"
    python -m shared.outbox_worker --once >/dev/null || echo "WARN outbox pass failed"
    if (( i % 3 == 0 )); then
        python -m shared.recording_storage --once    || echo "WARN recsync pass failed"
    fi
    if (( i % 12 == 0 )); then
        python -m shared.log_shipper --once          || echo "WARN logship pass failed"
    fi
    i=$((i + 1))
    sleep "$TICK"
done
