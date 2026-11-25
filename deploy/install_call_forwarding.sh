#!/usr/bin/env bash
# One-time FreeSWITCH setup for conditional call forwarding (RUNBOOK §84).
#
#   sudo bash deploy/install_call_forwarding.sh            # install (idempotent)
#   sudo bash deploy/install_call_forwarding.sh --uninstall
#
# What it does — and the ONLY change to the live call path:
#   1. creates /etc/freeswitch/dialplan/public_forward/ (one file per number
#      switched to "ring my phone first"; written by the dashboard) with an
#      empty placeholder, and /etc/freeswitch/lescall_prompts/ (voice prompts);
#   2. backs up dialplan/public.xml, then adds ONE include line directly above
#      the plivo_inbound catch-all;
#   3. reloadxml. If FreeSWITCH rejects it, the backup is restored and
#      reloaded again, and the script exits non-zero.
# With the folder empty (no number forwarding) every call routes exactly as
# before. Uninstall removes the include line; the folders are left in place.
set -euo pipefail

# FWD_TEST=1 + FS_DIR=<tmp copy>: exercise the script without root, chown
# or a live reload (used by tests/test_call_forwarding_install.py).
FS_DIR="${FS_DIR:-/etc/freeswitch}"
TEST="${FWD_TEST:-}"
DP_DIR=$FS_DIR/dialplan
FWD_DIR=$DP_DIR/public_forward
PROMPT_DIR=$FS_DIR/lescall_prompts
PUBLIC=$DP_DIR/public.xml
MARK="LesCall call forwarding (RUNBOOK §84)"
INCLUDE_LINE='    <X-PRE-PROCESS cmd="include" data="/etc/freeswitch/dialplan/public_forward/*.xml"/>'

[[ -n "$TEST" || $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

reloadxml() {
    if [[ -n "$TEST" ]]; then echo "${FWD_TEST_RELOAD:-+OK [Success]}"; return; fi
    docker exec -w /home/angelo/lescall lescall-api python -c \
        "import sys; sys.path.insert(0,'.'); from shared.fs_esl import fs_api; print(fs_api('reloadxml', timeout=20).strip())"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    if grep -q "$MARK" "$PUBLIC"; then
        cp -p "$PUBLIC" "$PUBLIC.pre-forwarding-uninstall.$(date +%Y%m%d-%H%M%S).bak"
        sed -i "/$MARK/d; \#public_forward/\*\.xml#d" "$PUBLIC"
        echo "include line removed"
    else
        echo "include line not present — nothing to remove"
    fi
    echo "reloadxml: $(reloadxml)"
    exit 0
fi

if [[ -n "$TEST" ]]; then install -d -m 2775 "$FWD_DIR" "$PROMPT_DIR"
else install -d -o freeswitch -g freeswitch -m 2775 "$FWD_DIR" "$PROMPT_DIR"; fi
if [[ ! -f "$FWD_DIR/00_placeholder.xml" ]]; then
    # FreeSWITCH needs the include glob to match at least one file.
    printf '<include>\n  <!-- %s: keeps the include glob non-empty. -->\n</include>\n' "$MARK" \
        > "$FWD_DIR/00_placeholder.xml"
    [[ -n "$TEST" ]] || chown freeswitch:freeswitch "$FWD_DIR/00_placeholder.xml"
    chmod 664 "$FWD_DIR/00_placeholder.xml"
fi
echo "folders ready: $FWD_DIR $PROMPT_DIR"

if grep -q "public_forward/\*\.xml" "$PUBLIC"; then
    echo "include line already present in $PUBLIC"
else
    grep -q '<extension name="plivo_inbound">' "$PUBLIC" \
        || { echo "plivo_inbound extension not found in $PUBLIC — aborting, nothing changed" >&2; exit 1; }
    BACKUP="$PUBLIC.pre-forwarding.$(date +%Y%m%d-%H%M%S).bak"
    cp -p "$PUBLIC" "$BACKUP"
    sed -i "s#^\(\s*\)<extension name=\"plivo_inbound\">#    <!-- $MARK: numbers set to \"ring my phone first\". Must stay ABOVE plivo_inbound. Empty folder = no effect. -->\n$INCLUDE_LINE\n\n&#" "$PUBLIC"
    echo "include line added (backup: $BACKUP)"
    OUT="$(reloadxml || true)"
    echo "reloadxml: $OUT"
    if [[ "$OUT" != *"+OK"* ]]; then
        echo "FreeSWITCH did not accept the change — restoring the backup" >&2
        cp -p "$BACKUP" "$PUBLIC"
        echo "reloadxml (restored): $(reloadxml || true)" >&2
        exit 1
    fi
fi
echo "done. Forwarding switches on per number from the dashboard (Telephony page)."
