#!/usr/bin/env python3
"""Generate the voice-preview samples served at /api/voices/sample/<speaker>.

One short bilingual line per Bulbul v3 speaker the platform offers, written
to data/voice_samples/<speaker>.wav. Run once (and re-run only when Sarvam
adds a speaker or the sample script changes) — each run costs real TTS
credits (~₹2 for the full set):

    cd /home/angelo/lescall && python3 deploy/generate_voice_samples.py

The speaker list mirrors _MALE_VOICES/_FEMALE_VOICES in
dashboard/agent_copilot_api.py — keep them in sync.
"""
import base64
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "data" / "voice_samples"

# speaker -> (gender, sample line). Curated bulbul:v3 subset the copilot
# offers (the full v3 roster is 37 voices — see voice_catalog.py). Hindi verb
# forms are gendered — दूँगा (m) vs दूँगी (f) — so the line is per-gender.
VOICES = {
    "shubh":  ("male",   "Hello! I'm Shubh. नमस्ते, आपकी कॉल्स पर मैं ऐसा सुनाई दूँगा।"),
    "aditya": ("male",   "Hello! I'm Aditya. नमस्ते, आपकी कॉल्स पर मैं ऐसा सुनाई दूँगा।"),
    "amit":   ("male",   "Hello! I'm Amit. नमस्ते, आपकी कॉल्स पर मैं ऐसा सुनाई दूँगा।"),
    "kabir":  ("male",   "Hello! I'm Kabir. नमस्ते, आपकी कॉल्स पर मैं ऐसा सुनाई दूँगा।"),
    "simran": ("female", "Hello! I'm Simran. नमस्ते, आपकी कॉल्स पर मैं ऐसी सुनाई दूँगी।"),
    "priya":  ("female", "Hello! I'm Priya. नमस्ते, आपकी कॉल्स पर मैं ऐसी सुनाई दूँगी।"),
    "neha":   ("female", "Hello! I'm Neha. नमस्ते, आपकी कॉल्स पर मैं ऐसी सुनाई दूँगी।"),
    "shruti": ("female", "Hello! I'm Shruti. नमस्ते, आपकी कॉल्स पर मैं ऐसी सुनाई दूँगी।"),
}


def _api_key() -> str:
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        env = REPO / "calling-agent" / ".env"
        m = re.search(r"^SARVAM_API_KEY=(\S+)", env.read_text(), re.M)
        key = m.group(1) if m else ""
    if not key:
        sys.exit("SARVAM_API_KEY not found (env or calling-agent/.env)")
    return key


def main() -> None:
    key = _api_key()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for speaker, (gender, text) in VOICES.items():
        out = OUT_DIR / f"{speaker}.wav"
        if out.exists() and out.stat().st_size > 10_000:
            print(f"  {speaker}: exists ({out.stat().st_size}b) — skipping")
            continue
        req = urllib.request.Request(
            "https://api.sarvam.ai/text-to-speech",
            data=json.dumps({
                "text": text,
                "target_language_code": "hi-IN",  # codemix handles the English lead-in
                "speaker": speaker,
                "model": "bulbul:v3",
                "pace": 1.0,
                "speech_sample_rate": 16000,
            }).encode(),
            headers={"api-subscription-key": key,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        wav = base64.b64decode(body["audios"][0])
        out.write_bytes(wav)
        print(f"  {speaker} ({gender}): {len(wav)} bytes -> {out}")
    # index.json is what /api/voices serves — speaker + gender, in the
    # curated order above.
    (OUT_DIR / "index.json").write_text(json.dumps(
        [{"speaker": s, "gender": g} for s, (g, _) in VOICES.items()],
        indent=2))
    print(f"  index.json -> {OUT_DIR / 'index.json'}")


if __name__ == "__main__":
    main()
