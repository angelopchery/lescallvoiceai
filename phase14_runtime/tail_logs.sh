#!/bin/bash
# tail_logs.sh - Live observability for Phase 14

RUNTIME_DIR="/home/angelo/lescall/phase14_runtime"
LATEST_RUN="$RUNTIME_DIR/logs/latest"

if [ ! -d "$LATEST_RUN" ]; then
    echo "No latest run found in $LATEST_RUN"
    exit 1
fi

echo "Tailing live logs from $LATEST_RUN..."
echo "Use Ctrl+C to stop monitoring."
echo "------------------------------------------------"

# ANSI Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Files to tail
FILES=("$LATEST_RUN/socket_server.log" "$LATEST_RUN/ws_audio_receiver.log")

tail -qf "${FILES[@]}" | awk -v red="$RED" -v green="$GREEN" -v yellow="$YELLOW" -v blue="$BLUE" -v magenta="$MAGENTA" -v cyan="$CYAN" -v nc="$NC" '
{
    line = $0
    p = "[INFO]"
    color = nc

    # Identify context based on content if filename prefix is lost by tail -qf
    if (line ~ /ESL|socket_server|PLAYBACK_LISTENER/) {
        p = "[ESL]"
        color = blue
    } else if (line ~ /WS CONNECT|WS DISCONNECT|connection open|connection closed|ws_audio_receiver/) {
        p = "[WS]"
        color = cyan
    } else if (line ~ /call_bridge|PIPELINE|Session started/) {
        p = "[CALL]"
        color = green
    } else if (line ~ /STT|transcript/) {
        p = "[STT]"
        color = magenta
    }

    # High priority content markers
    if (line ~ /ERROR|CRITICAL|Failed|failed|Error|error|Exception|Traceback/) {
        p = "[ERROR]"
        color = red
    } else if (line ~ /PLAYBACK|Injected playback/) {
        p = "[PLAYBACK]"
        color = yellow
    } else if (line ~ /LLM|TTS|Conversation/) {
        p = "[AI]"
        color = green
    }

    # Print with color
    printf "%s%s %s%s\n", color, p, line, nc
    fflush()
}'
