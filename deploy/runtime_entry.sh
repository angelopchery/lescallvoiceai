#!/usr/bin/env bash
# Container entrypoint for the telephony runtime: socket_server (ESL control,
# :9000 + :9004 playback) and ws_audio_receiver (media WS :9001 + AI bridge).
# Both share the host network namespace, so FreeSWITCH's existing loopback
# config keeps working unchanged. If either process dies the container exits
# and Docker's restart policy brings the pair back (they are a unit — the
# bridge dials socket_server's playback port).
set -euo pipefail
cd /home/angelo/lescall

mkdir -p phase14_runtime/logs/docker
LOG_DIR=phase14_runtime/logs/docker
ln -sfn docker phase14_runtime/logs/latest

set -a
[ -f calling-agent/.env ] && source calling-agent/.env
set +a

python socket_server.py >>"$LOG_DIR/socket_server.log" 2>&1 &
SOCK_PID=$!
echo "$SOCK_PID" > /tmp/socket_server.pid

python ws_audio_receiver.py --sample-rate "${TELEPHONY_SAMPLE_RATE:-8000}" \
    >>"$LOG_DIR/ws_audio_receiver.log" 2>&1 &
WS_PID=$!
echo "$WS_PID" > /tmp/ws_audio_receiver.pid

echo "runtime up: socket_server=$SOCK_PID ws_audio_receiver=$WS_PID"
trap 'kill $SOCK_PID $WS_PID 2>/dev/null || true' TERM INT

# Exit when EITHER dies so docker restarts the pair together.
wait -n $SOCK_PID $WS_PID
EXIT_CODE=$?
echo "runtime process exited ($EXIT_CODE); stopping pair for clean restart"
kill $SOCK_PID $WS_PID 2>/dev/null || true
exit "$EXIT_CODE"
