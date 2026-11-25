#!/bin/bash
# start_all.sh - Deterministic startup for Phase 14 runtime

RUNTIME_DIR="/home/angelo/lescall/phase14_runtime"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RUN_DIR="$RUNTIME_DIR/logs/run_$TIMESTAMP"
mkdir -p "$RUN_DIR"

# Symlink "latest"
rm -f "$RUNTIME_DIR/logs/latest"
ln -s "$RUN_DIR" "$RUNTIME_DIR/logs/latest"

# Prune old run dirs (keep newest 15) — logs once grew to 9.3 GB unbounded.
ls -dt "$RUNTIME_DIR"/logs/run_* 2>/dev/null | tail -n +16 | xargs -r rm -rf 2>/dev/null

echo "Starting Phase 14 Runtime..."
echo "Log directory: $RUN_DIR"

# 1. Reset runtime first
/bin/bash "$RUNTIME_DIR/reset_runtime.sh"
if [ $? -ne 0 ]; then
    echo "ERROR: Runtime reset failed. Aborting startup."
    exit 1
fi

# Function to check if a port is listening
wait_for_port() {
    local port=$1
    local name=$2
    local timeout=10
    local count=0
    echo "Waiting for $name to bind to port $port..."
    while ! ss -ltn | grep -q ":$port "; do
        sleep 1
        count=$((count + 1))
        if [ $count -ge $timeout ]; then
            echo "[FAIL] $name failed to bind to port $port within ${timeout}s"
            return 1
        fi
    done
    echo "[OK] $name listening on $port"
    return 0
}

# Change to /home/angelo so relative imports and 'calling-agent' path work
cd /home/angelo/lescall

# 2. Start socket_server.py (ESL Controller + Playback Listener)
echo "[1/2] Starting Socket Server (System Python)..."
nohup python3 /home/angelo/lescall/socket_server.py >> "$RUN_DIR/socket_server.log" 2>&1 &
SS_PID=$!
echo $SS_PID > "$RUN_DIR/socket_server.pid"

if ! wait_for_port 9000 "socket_server"; then
    echo "ERROR: socket_server.py failed to start or bind."
    cat "$RUN_DIR/socket_server.log"
    exit 1
fi

# 3. Start ws_audio_receiver.py (WebSocket Media Receiver + AI Bridge)
echo "[2/2] Starting WS Audio Receiver (Venv Python)..."
# Load environment if needed (assuming .env is in calling-agent)
if [ -f "/home/angelo/lescall/calling-agent/.env" ]; then
    set -a
    source /home/angelo/lescall/calling-agent/.env
    set +a
fi

nohup /home/angelo/lescall/calling-agent/venv/bin/python /home/angelo/lescall/ws_audio_receiver.py --sample-rate 8000 >> "$RUN_DIR/ws_audio_receiver.log" 2>&1 &
WS_PID=$!
echo $WS_PID > "$RUN_DIR/ws_audio_receiver.pid"

if ! wait_for_port 9001 "ws_audio_receiver"; then
    echo "ERROR: ws_audio_receiver.py failed to start or bind."
    cat "$RUN_DIR/ws_audio_receiver.log"
    exit 1
fi

# 4. Create a filtered view for call_bridge logs (they go to ws_audio_receiver.log)
# Wrapped in `bash -c` so the redirect below applies to tail's stderr too —
# otherwise tail -f's stderr inherits the caller's stdout/stderr pipe (e.g. the
# dashboard's subprocess pipe) and, since tail -f never exits, that pipe never
# reaches EOF, which makes the dashboard think a start/stop command is stuck
# running forever.
nohup bash -c 'tail -f "'"$RUN_DIR"'/ws_audio_receiver.log" | grep --line-buffered "call_bridge"' >> "$RUN_DIR/call_bridge.log" 2>&1 &
FILTER_PID=$!
echo $FILTER_PID > "$RUN_DIR/call_bridge_filter.pid"

echo "------------------------------------------------"
echo "All mandatory services started successfully."
echo "Use ./tail_logs.sh to monitor."
exit 0
