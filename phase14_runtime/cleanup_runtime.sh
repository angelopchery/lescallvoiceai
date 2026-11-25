#!/bin/bash
# cleanup_runtime.sh - Clean up old logs and artifacts

RUNTIME_DIR="/home/angelo/lescall/phase14_runtime"

echo "Cleaning up Phase 14 runtime..."

# Keep only the last 5 runs
if [ -d "$RUNTIME_DIR/logs" ]; then
    ls -1dt "$RUNTIME_DIR/logs/run_"* 2>/dev/null | tail -n +6 | xargs rm -rf 2>/dev/null
fi

# Clean up /tmp directories
rm -rf /tmp/ai_telephony_playback/*
rm -rf /tmp/ai_responses/*
rm -rf /tmp/orch_transcripts/*

echo "Cleanup complete."
