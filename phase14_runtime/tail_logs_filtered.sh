#!/bin/bash
# tail_logs_filtered.sh - Critical errors, caller transcripts, and agent responses only

RUNTIME_DIR="/home/angelo/lescall/phase14_runtime"
LATEST_RUN="$RUNTIME_DIR/logs/latest"

if [ ! -d "$LATEST_RUN" ]; then
    echo "No latest run found in $LATEST_RUN"
    exit 1
fi

echo "Filtering logs from $LATEST_RUN (errors, transcripts, AI responses)..."
echo "Use Ctrl+C to stop monitoring."
echo "------------------------------------------------"

FILES=("$LATEST_RUN/socket_server.log" "$LATEST_RUN/ws_audio_receiver.log")

tail -qf "${FILES[@]}" | awk '
/ERROR|CRITICAL|Failed|failed|Error|error|Exception|Traceback/ { printf "\033[0;31m[ERROR] %s\033[0m\n", $0; fflush(); next }
/transcript|STT/                              { printf "\033[0;35m[STT]   %s\033[0m\n", $0; fflush(); next }
/LLM|TTS|Conversation/                                          { printf "\033[0;32m[AI]    %s\033[0m\n", $0; fflush(); next }
/CREDITS/                                                        { printf "\033[0;33m[CREDIT] %s\033[0m\n", $0; fflush(); next }
'
