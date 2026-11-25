#!/usr/bin/env bash
# Post-cutover verification for the attached-service-account switch (RUNBOOK §6b).
#
# Run ON the VM after:
#   gcloud compute instances stop  lescall-agent --zone asia-south1-c
#   gcloud compute instances set-service-account lescall-agent --zone asia-south1-c \
#     --service-account lescall-recsync@lescall.iam.gserviceaccount.com --scopes cloud-platform
#   gcloud compute instances start lescall-agent --zone asia-south1-c
#
#   bash deploy/verify_gcs_cutover.sh
#
# Read-only except for one throwaway .healthcheck blob and the backlog drain
# (which is the point). Does NOT delete the old ADC file — that is step 4,
# a human decision made only after this script is green, because while the
# file exists the app-layer rollback needs no VM stop.
set -uo pipefail
cd "$(dirname "$0")/.."

SA_WANT="lescall-recsync@lescall.iam.gserviceaccount.com"
BUCKET="${GCS_RECORDINGS_BUCKET:-lescall-recordings}"
COMPOSE="deploy/docker-compose.services.yml"
MD="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default"
fails=0

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }

step "1/6  Attached service account"
email=$(curl -s -H Metadata-Flavor:Google "$MD/email" || echo "")
if [[ "$email" == "$SA_WANT" ]]; then
    ok "$email"
else
    bad "metadata says '${email:-<unreachable>}' — expected $SA_WANT"
    echo "        The set-service-account step did not take. Nothing below will pass."
fi

step "2/6  Instance scopes"
scopes=$(curl -s -H Metadata-Flavor:Google "$MD/scopes" || echo "")
if grep -q "auth/cloud-platform" <<<"$scopes"; then
    ok "cloud-platform present"
else
    bad "cloud-platform missing. Scopes: ${scopes//$'\n'/ }"
    echo "        Legacy scopes CAP IAM — devstorage.read_only silently downgrades"
    echo "        every write no matter which roles the SA holds."
fi

step "3/6  Containers recreated (stale credential env gone)"
# restart:unless-stopped restarts the EXISTING container objects with the env
# baked in at create time — a VM reboot alone does NOT apply a compose edit.
for c in lescall-api lescall-runtime lescall-workers; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
        bad "$c is not running"
        continue
    fi
    gac=$(docker inspect "$c" --format \
        '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | grep '^GOOGLE_APPLICATION_CREDENTIALS=' || true)
    mnt=$(docker inspect "$c" --format \
        '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null \
        | grep 'config/gcloud' || true)
    if [[ -z "$gac" && -z "$mnt" ]]; then
        ok "$c clean (no GAC env, no gcloud mount)"
    else
        bad "$c still carries ${gac:-}${mnt:+ mount:$mnt}"
        echo "        Fix: docker compose -f $COMPOSE up -d   (recreate, not restart)"
    fi
done

step "4/6  GCS WRITE round-trip as the attached SA"
# A read would succeed under devstorage.read_only too — only a write proves
# the scope change landed. This is the check that actually matters.
if docker exec lescall-workers python -c "
from google.cloud import storage
b = storage.Client().bucket('$BUCKET')
blob = b.blob('.healthcheck')
blob.upload_from_string('ok')
blob.delete()
print('   write+delete OK on $BUCKET')
" 2>&1 | sed 's/^/   /'; then
    ok "bucket is writable"
else
    bad "write failed — see the traceback above"
    echo "        RefreshError  → credential precedence: check steps 1-3."
    echo "        403 / Forbidden → SA lacks roles/storage.objectAdmin on $BUCKET."
fi

step "5/6  Drain the backlog"
before=$(docker exec lescall-pg psql -U lescall -d lescall -tAc \
    "SELECT count(gcs_uploaded_at) FROM calls;" 2>/dev/null | tr -d ' ')
echo "   stamped before: ${before:-?}   (baseline at cutover: 41 of 311)"
echo "   uploading — this walks ~270 calls, give it a few minutes..."
docker exec lescall-workers python -m shared.recording_storage --once 2>&1 \
    | tail -5 | sed 's/^/   /'

step "6/6  Result"
docker exec lescall-pg psql -U lescall -d lescall -c \
    "SELECT count(*) AS total, count(gcs_uploaded_at) AS uploaded,
            max(gcs_uploaded_at) AS last_upload FROM calls;" 2>/dev/null \
    | sed 's/^/   /'
after=$(docker exec lescall-pg psql -U lescall -d lescall -tAc \
    "SELECT count(gcs_uploaded_at) FROM calls;" 2>/dev/null | tr -d ' ')
if [[ -n "${after:-}" && -n "${before:-}" && "$after" -gt "$before" ]]; then
    ok "uploaded $((after - before)) new recordings this pass"
else
    bad "stamp count did not move ($before → ${after:-?})"
fi

df -h / | tail -1 | sed 's/^/   disk: /'
du -sh recordings 2>/dev/null | sed 's/^/   local recordings: /'

echo
if (( fails == 0 )); then
    printf '\033[32m✔ CUTOVER VERIFIED\033[0m — auth is the attached SA via metadata.\n'
    cat <<'NEXT'

Remaining steps, in order:

  1. Retire the host-side stale credential. Host processes (lescall-sentinel,
     lescall-recsync.timer) set no env var, so they fall through to the
     well-known ADC path and keep using the dead user token until it is gone:

         rm -f ~/.config/gcloud/application_default_credentials.json

     Do this only now, with the checks above green: while that file exists,
     `git checkout deploy/docker-compose.services.yml deploy/.pg_env &&
     docker compose -f deploy/docker-compose.services.yml up -d` is a full
     app-layer rollback needing no VM stop.

  2. Confirm host-side auth recovered (no gcs-auth alert on the next check):

         journalctl -u lescall-sentinel -n 30 --no-pager | grep -i gcs

  3. Drop the duplicate host timer — the workers container already runs this
     pass every 15 min:

         sudo systemctl disable --now lescall-recsync.timer

  4. Spot-check a download link end-to-end: open any recording from the
     dashboard whose local WAV was already deleted (the 41 pre-outage calls).
     That exercises the GCS read fallback, which has been dead since 07-14.
NEXT
else
    printf '\033[31m✘ %d CHECK(S) FAILED\033[0m — do NOT delete the ADC file.\n' "$fails"
    cat <<'ROLLBACK'

Nothing is lost: cleanup_local deletes a local WAV only after a successful
upload is stamped, so the worst case is uploads stay broken as they were.

App-layer rollback (no VM stop, seconds):
    git checkout deploy/docker-compose.services.yml deploy/.pg_env
    docker compose -f deploy/docker-compose.services.yml up -d

Full SA revert and the rest of the procedure: RUNBOOK §6b.
ROLLBACK
    exit 1
fi
