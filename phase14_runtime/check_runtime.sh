#!/bin/bash
# check_runtime.sh - Verify health of Phase 14 runtime

RUNTIME_DIR="/home/angelo/lescall/phase14_runtime"
LATEST_LOGS="$RUNTIME_DIR/logs/latest"

echo "--- Phase 14 Runtime Status ---"

if [ ! -d "$LATEST_LOGS" ]; then
    echo "[FAIL] No latest logs directory found at $LATEST_LOGS"
    exit 1
fi

# 1. Check PIDs from files
echo "Checking PIDs from log directory..."
PASS=1
for pid_file in "$LATEST_LOGS"/*.pid; do
    [ -e "$pid_file" ] || continue
    name=$(basename "$pid_file" .pid)
    pid=$(cat "$pid_file")
    if ps -p "$pid" > /dev/null; then
        echo "[OK] $name (PID $pid) is running."
    else
        echo "[FAIL] $name (PID $pid) is NOT running!"
        PASS=0
    fi
done

# 2. Check Listening Ports
echo "--- Listening Ports ---"
ss -ltnp | grep -E '9000|9001|9004'

# 3. Verify exactly one listener per port
echo "--- Port Ownership Verification ---"
PORTS=(9000 9001 9004)

for port in "${PORTS[@]}"; do
    COUNT=$(ss -ltn | grep -c ":$port ")
    if [ "$COUNT" -eq 1 ]; then
        echo "[OK] Port $port has exactly one listener."
    elif [ "$COUNT" -gt 1 ]; then
        echo "[FAIL] Port $port has MULTIPLE listeners ($COUNT)!"
        PASS=0
    else
        echo "[FAIL] Port $port has NO listeners!"
        PASS=0
    fi
done

# 4. Verify ws_audio_receiver python environment
echo "--- Process Environment Verification ---"
WS_PID=$(cat "$LATEST_LOGS/ws_audio_receiver.pid" 2>/dev/null)
if [ -n "$WS_PID" ] && ps -p "$WS_PID" > /dev/null; then
    CMDLINE=$(cat /proc/$WS_PID/cmdline | tr '\0' ' ')
    if [[ "$CMDLINE" == *"/calling-agent/venv/bin/python"* ]]; then
        echo "[OK] ws_audio_receiver command line uses venv: $CMDLINE"
    else
        echo "[FAIL] ws_audio_receiver command line does NOT use venv! (Cmd: $CMDLINE)"
        PASS=0
    fi
else
    echo "[FAIL] ws_audio_receiver PID not found or process dead."
    PASS=0
fi

echo "------------------------------------------------"
if [ $PASS -eq 1 ]; then
    echo "OVERALL STATUS: PASS"
    exit 0
else
    echo "OVERALL STATUS: FAIL"
    exit 1
fi
