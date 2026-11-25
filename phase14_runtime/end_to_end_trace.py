import sys
import os

log_dir = "/home/angelo/lescall/phase14_runtime/logs/latest"
logs = ["socket_server.log", "ws_audio_receiver.log", "call_bridge.log"]
content = ""
for l in logs:
    p = os.path.join(log_dir, l)
    if os.path.isfile(p):
        with open(p, "r") as f:
            content += f.read()

def chk(text, name, msg):
    if text in content:
        print(f"✓ {name}")
        return True
    else:
        print(f"✗ {name} (Missing. {msg})")
        return False

print("========================================================")
print("END-TO-END CALL TRACE")
print("========================================================")

c_start = chk("CALL_START", "CALL_START", "Check SIP/dialplan")
c_answer = chk("Answer-State: answered", "CHANNEL_ANSWER", "Check dialplan 'answer' app")
c_attach = chk("MEDIA_ATTACH", "MEDIA_ATTACH", "Check mod_audio_stream / websocket url")
c_audio = chk("MEDIA_STREAM_STARTED", "AUDIO_RECEIVED", "Check FreeSWITCH media path")
c_stt = chk("[STT] ", "STT", "Check Sarvam/audio buffers")
c_llm = chk("[ENGINE] ", "LLM", "Check conversation engine")
c_tts = chk("[TTS:Sarvam] ", "TTS", "Check TTS provider")
c_wav = chk("PLAYBACK_WAV", "WAV_GENERATED", "Check telephony_sink WAV creation")
c_send = chk("PLAYBACK_SEND", "PLAYBACK_SEND", "Check orchestrator playback loop")
c_done = chk("PLAYBACK_COMPLETE", "PLAYBACK_COMPLETE", "Check socket_server ESL injection")

print("========================================================")
if c_done:
    print("ALL STAGES SUCCESSFUL! End-to-end trace complete.")
else:
    print("EXACT STAGE OF FAILURE: Trace did not reach PLAYBACK_COMPLETE.")
    print("Please check the first '✗' above.")
