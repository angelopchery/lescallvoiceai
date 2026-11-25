#!/usr/bin/env bash
# Does this VM have a service account that can WRITE the platform's buckets
# by itself — and if not, give it one.
#
#   deploy/provision_vm_sa.sh              check: what the VM can do RIGHT NOW (no login needed)
#   deploy/provision_vm_sa.sh --provision  create / bind what is missing (needs an OWNER's gcloud login)
#
# The check runs AS THE VM — a token from the metadata server, the real GCS
# JSON API, a real object written and deleted per bucket — so it answers the
# question the way lescall-workers experiences it, not the way IAM describes
# it. It is the same probe the sentinel now runs hourly (shared/sentinel.py
# check_gcs, RUNBOOK §59).
#
# Provisioning cannot run as the VM: the attached SA holds object-level bucket
# roles only, and the project has the Cloud Resource Manager API disabled, so
# the VM can neither create a service account nor grant itself anything. That
# step needs a human who owns the project — `gcloud auth login` in this shell,
# then `--provision`. Nothing here creates an SA KEY: org policy blocks that,
# and the attached-SA design needs none (deploy/.pg_env, RUNBOOK §6b).
set -euo pipefail

PROJECT="${GCP_PROJECT:-lescall}"
SA_NAME="${VM_SA_NAME:-lescall-recsync}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
BUCKETS=("${GCS_RECORDINGS_BUCKET:-lescall-recordings}" "${GCS_LOGS_BUCKET:-lescall-logs}")
MD="http://metadata.google.internal/computeMetadata/v1"
H="Metadata-Flavor: Google"

md() { curl -sf -H "$H" "$MD/$1" 2>/dev/null || true; }
vm_token() {
    md instance/service-accounts/default/token \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
}
urlenc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"; }

check() {
    local attached; attached=$(md instance/service-accounts/default/email)
    if [[ -z "$attached" ]]; then
        echo "NO service account is attached to this VM (metadata server offers none)."
        echo "Attach one: see the --provision output, or RUNBOOK §6b."
        return 1
    fi
    echo "attached SA : $attached"
    echo "scopes      : $(md instance/service-accounts/default/scopes | tr '\n' ' ')"
    [[ "$attached" == "$SA" ]] || echo "NOTE: attached SA is not $SA — checking the one that is attached."
    local tok rc=0 body; tok=$(vm_token); body=$(mktemp)
    for b in "${BUCKETS[@]}"; do
        local obj="_sentinel/provision-check/$(hostname).txt" code
        code=$(curl -s -o "$body" -w '%{http_code}' -X POST \
            -H "Authorization: Bearer $tok" -H "Content-Type: text/plain" \
            --data-binary "provision check $(date -u +%FT%TZ)" \
            "https://storage.googleapis.com/upload/storage/v1/b/$b/o?uploadType=media&name=$(urlenc "$obj")")
        if [[ "$code" == "200" ]]; then
            curl -s -o /dev/null -X DELETE -H "Authorization: Bearer $tok" \
                "https://storage.googleapis.com/storage/v1/b/$b/o/$(urlenc "$obj")" || true
            echo "gs://$b : WRITE ok"
        else
            local why; why=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["error"]["message"])' "$body" 2>/dev/null | head -c 220 || true)
            echo "gs://$b : WRITE FAILED (HTTP $code) ${why:-$(head -c 220 "$body")}"
            case "$why" in
                *"billing account"*) echo "            -> BILLING, not IAM: Cloud Console -> Billing -> the account linked to project $PROJECT. No SA change will help." ;;
                *) echo "            -> the attached SA lacks roles/storage.objectAdmin on this bucket (or the bucket is gone). Run: $0 --provision" ;;
            esac
            rc=1
        fi
    done
    rm -f "$body"
    return $rc
}

provision() {
    local who
    who=$(gcloud auth print-access-token >/dev/null 2>&1 && gcloud config get-value account 2>/dev/null || true)
    if [[ -z "$who" || "$who" == *gserviceaccount.com ]]; then
        echo "No working OWNER login in gcloud (active: '${who:-none}')."
        echo "Run, as a human who owns project $PROJECT:   gcloud auth login"
        echo "then re-run:   $0 --provision"
        exit 2
    fi
    echo "acting as $who on project $PROJECT"
    gcloud services enable iam.googleapis.com cloudresourcemanager.googleapis.com --project "$PROJECT"
    if gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
        echo "SA exists   : $SA"
    else
        gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
            --display-name "LesCall VM: recordings + logs to GCS"
        echo "SA created  : $SA"
    fi
    for b in "${BUCKETS[@]}"; do
        gcloud storage buckets add-iam-policy-binding "gs://$b" \
            --member "serviceAccount:$SA" --role roles/storage.objectAdmin >/dev/null
        echo "gs://$b : roles/storage.objectAdmin -> $SA"
    done
    local attached; attached=$(md instance/service-accounts/default/email)
    if [[ "$attached" != "$SA" ]]; then
        local inst zone
        inst=$(md instance/name); zone=$(md instance/zone | awk -F/ '{print $NF}')
        cat <<EOF

This VM runs as '${attached:-no SA}', not '$SA'. Attaching a service account
needs the instance STOPPED — that is the phone lines down — so it is NOT done
here. When you can take the outage:
  gcloud compute instances stop $inst --zone $zone
  gcloud compute instances set-service-account $inst --zone $zone \\
      --service-account $SA --scopes cloud-platform
  gcloud compute instances start $inst --zone $zone
EOF
    fi
    echo; echo "re-checking as the VM:"; check
}

case "${1:-}" in
    --provision) provision ;;
    ""|--check)  check ;;
    *) echo "usage: $0 [--check|--provision]"; exit 64 ;;
esac
