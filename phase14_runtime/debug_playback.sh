#!/bin/bash
# Phase 17: Telephony Playback Debug Tool

PLAYBACK_DIR="/tmp/ai_telephony_playback"
LOG_FILE="phase14_runtime/logs/call_bridge.log"

echo "========================================================"
echo "TELEPHONY PLAYBACK DEBUGGER"
echo "========================================================"

if [ ! -d "$PLAYBACK_DIR" ]; then
    echo "Directory $PLAYBACK_DIR does not exist."
else
    echo "--- LATEST 10 PLAYBACK WAVS ---"
    ls -lt "$PLAYBACK_DIR"/*.wav 2>/dev/null | head -n 10
    
    echo ""
    echo "--- DURATIONS (Latest 5) ---"
    for f in $(ls -t "$PLAYBACK_DIR"/*.wav 2>/dev/null | head -n 5); do
        if command -v ffprobe >/dev/null 2>&1; then
            duration=$(ffprobe -i "$f" -show_entries format=duration -v quiet -of csv="p=0")
            size=$(stat -c%s "$f")
            echo "$(basename $f): ${duration}s (${size} bytes)"
        else
            size=$(stat -c%s "$f")
            echo "$(basename $f): (ffprobe missing) ${size} bytes"
        fi
    done
fi

echo ""
echo "--- LATEST PLAYBACK LOGS (Tail 100, Grepped) ---"
if [ -f "$LOG_FILE" ]; then
    tail -n 100 "$LOG_FILE" | grep -E "PLAYBACK|BARGE-IN|VAD|TURN"
else
    # Try alternate location or grep all logs
    tail -n 100 phase14_runtime/logs/*.log 2>/dev/null | grep -E "PLAYBACK|BARGE-IN|VAD|TURN"
fi

echo "========================================================"
