#!/bin/bash
# reset_runtime.sh - Deterministically clean up Phase 14 runtime processes and ports

echo "--- Resetting Phase 14 Runtime ---"

# 1. Kill by script name
SCRIPTS=("ws_audio_receiver.py" "socket_server.py" "conversation_call_bridge.py")
for script in "${SCRIPTS[@]}"; do
    echo "Killing processes matching: $script"
    pkill -9 -f "$script"
done

# 2. Kill by port (9000, 9001, 9004)
PORTS=(9000 9001 9004)
for port in "${PORTS[@]}"; do
    echo "Checking port $port..."
    # Find PIDs using ss -ltnp
    PIDS=$(ss -ltnp | grep ":$port " | awk -F'pid=' '{print $2}' | cut -d, -f1)
    if [ -n "$PIDS" ]; then
        echo "Port $port is occupied by PIDs: $PIDS. Force killing..."
        echo "$PIDS" | xargs kill -9 2>/dev/null
    else
        echo "Port $port is free."
    fi
done

# 3. Final verification
echo "--- Final Port Verification ---"
STILL_OCCUPIED=0
for port in "${PORTS[@]}"; do
    if ss -ltn | grep -q ":$port "; then
        echo "[FAIL] Port $port is STILL occupied!"
        ss -ltnp | grep ":$port "
        STILL_OCCUPIED=1
    else
        echo "[OK] Port $port is clean."
    fi
done


if [ $STILL_OCCUPIED -ne 0 ]; then
    echo "ERROR: Could not free all ports. Reset failed."
    exit 1
fi

echo "--- Clean State Verified ---"
exit 0
