#!/bin/bash
echo "========================================================"
echo "END-TO-END CALL TRACE"
echo "========================================================"

LOG_DIR="/home/angelo/lescall/phase14_runtime/logs/latest"
if [ ! -d "$LOG_DIR" ]; then
    echo "No logs found."
    exit 1
fi

grep -E "CALL_START|CHANNEL_ANSWER|MEDIA_ATTACH|MEDIA_STREAM_STARTED|\[STT\] |\[ENGINE\] |\[TTS:Sarvam\] |PLAYBACK_WAV|PLAYBACK_ENQUEUE|PLAYBACK_SEND|PLAYBACK_COMPLETE" "$LOG_DIR/socket_server.log" "$LOG_DIR/ws_audio_receiver.log" "$LOG_DIR/call_bridge.log" 2>/dev/null | sort -k1,2 > /tmp/trace.tmp

cat /tmp/trace.tmp | while read -r line; do
    if [[ "$line" == *"CALL_START"* ]]; then echo "✓ CALL_START: Outbound ESL connected"; fi
    if [[ "$line" == *"CHANNEL_ANSWER"* ]]; then echo "✓ CHANNEL_ANSWER: Media path active"; fi
    if [[ "$line" == *"MEDIA_ATTACH"* ]]; then echo "✓ MEDIA_ATTACH: WebSocket receiver connected"; fi
    if [[ "$line" == *"MEDIA_STREAM_STARTED"* ]]; then echo "✓ AUDIO_RECEIVED: First binary PCM frame arrived"; fi
    if [[ "$line" == *"\[STT\] "* ]]; then echo "✓ STT: Transcription successful"; fi
    if [[ "$line" == *"\[ENGINE\] "* ]]; then echo "✓ LLM: Engine generated response"; fi
    if [[ "$line" == *"\[TTS:Sarvam\] "* ]]; then echo "✓ TTS: Sarvam TTS generation started"; fi
    if [[ "$line" == *"PLAYBACK_WAV"* ]]; then echo "✓ WAV_GENERATED: Audio file successfully created & validated"; fi
    if [[ "$line" == *"PLAYBACK_SEND"* ]]; then echo "✓ PLAYBACK_SEND: Orchestrator instructed FS to play"; fi
    if [[ "$line" == *"PLAYBACK_COMPLETE"* ]]; then echo "✓ PLAYBACK_COMPLETE: FS completed execution"; fi
done | awk '!seen[$0]++'

echo "========================================================"
