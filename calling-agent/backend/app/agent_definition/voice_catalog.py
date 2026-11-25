"""
Voice/language catalog — the single source of truth for what the platform's
TTS/STT layer accepts. Re-exported by schema.py and consumed by tts_sarvam.py
and the Copilot; nothing else should hardcode model or speaker names.

PLATFORM_LANGUAGES is deliberately generous (all Sarvam-supported Indic codes):
`from_dict` warn-drops only codes Sarvam cannot speak at all. The deep
conversational experience (fillers, register notes, deterministic responses)
is still authored for hi/en/gu — the interview sets that expectation.
"""

from __future__ import annotations

# The only TTS models Sarvam accepts; anything else 400s every utterance and
# the call goes silent. Platform default is bulbul:v3 unless explicitly set.
VALID_TTS_MODELS = ("bulbul:v2", "bulbul:v3-beta", "bulbul:v3")
DEFAULT_TTS_MODEL = "bulbul:v3"
DEFAULT_TTS_SPEAKER = "shubh"

# Short BCP-47 codes the runtime/STT/TTS chain can carry end-to-end.
PLATFORM_LANGUAGES = (
    "en", "hi", "gu", "mr", "ta", "te", "kn", "ml", "bn", "pa", "od",
)

# speaker -> {"gender": ..., "models": (...)} — informational (warn-only
# validation): Sarvam adds speakers faster than we ship, so an unknown
# speaker is a warning, never a rejection.
#
# The bulbul:v3 roster below is the OFFICIAL list (23 male + 14 female,
# docs.sarvam.ai, verified against the live API 2026-07-24 — the API 400s
# any other speaker on v3 and the call goes silent). The legacy block at the
# bottom is v2-only: those speakers were removed in v3, which is why the old
# catalog entries claiming VALID_TTS_MODELS for them were a dead-air trap.
_V3 = ("bulbul:v3-beta", "bulbul:v3")
_V3_MALE = (
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun",
    "manan", "sumit", "kabir", "aayan", "ashutosh", "advait", "anand",
    "tarun", "sunny", "mani", "gokul", "vijay", "mohit", "rehan", "soham",
)
_V3_FEMALE = (
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
)
SPEAKER_CATALOG: dict[str, dict] = {
    **{s: {"gender": "male", "models": _V3} for s in _V3_MALE},
    **{s: {"gender": "female", "models": _V3} for s in _V3_FEMALE},
    # bulbul:v2 legacy speakers (dropped in v3):
    "anushka":  {"gender": "female", "models": ("bulbul:v2",)},
    "manisha":  {"gender": "female", "models": ("bulbul:v2",)},
    "vidya":    {"gender": "female", "models": ("bulbul:v2",)},
    "abhilash": {"gender": "male",   "models": ("bulbul:v2",)},
    "karun":    {"gender": "male",   "models": ("bulbul:v2",)},
}


# Sarvam's per-language recommended speakers (best-practices CER tables,
# docs.sarvam.ai, captured 2026-07-30): not all speakers pronounce every
# language equally — shubh is a Hindi-tier voice and is NOT recommended for
# English (ratan/ishita are). Keyed (short_lang, gender). Used by the Copilot
# when generating tts_voices for NEW agents; existing agents are never
# rewritten. ONE speaker per agent across its languages — switching speakers
# mid-call changes the person, which is worse than an accent shift.
RECOMMENDED_SPEAKERS: dict[tuple[str, str], str] = {
    ("en", "male"): "ratan", ("en", "female"): "ishita",
    ("hi", "male"): "shubh", ("hi", "female"): "priya",
    ("gu", "male"): "ratan", ("gu", "female"): "priya",
    ("mr", "male"): "ratan", ("mr", "female"): "priya",
    ("ta", "male"): "ratan", ("ta", "female"): "ishita",
    ("te", "male"): "shubh", ("te", "female"): "neha",
    ("kn", "male"): "shubh", ("kn", "female"): "neha",
    ("bn", "male"): "rehan", ("bn", "female"): "roopa",
    ("pa", "male"): "mani",  ("pa", "female"): "roopa",
    ("od", "male"): "shubh", ("od", "female"): "ritu",
    ("ml", "male"): "shubh", ("ml", "female"): "pooja",
}


def recommended_speaker(language: str, gender: str = "female") -> str:
    """Best default speaker for a language+gender per Sarvam's CER tables.
    Falls back to the top overall voices (priya / mani) for unknown codes."""
    g = (gender or "female").strip().lower()
    key = ((language or "en").strip().lower(), g)
    return RECOMMENDED_SPEAKERS.get(key, "priya" if g == "female" else "mani")


def speaker_gender(speaker: str) -> str:
    """'male' / 'female' / '' (unknown speaker)."""
    return SPEAKER_CATALOG.get((speaker or "").strip().lower(), {}).get("gender", "")


def speaker_models(speaker: str) -> tuple | None:
    """Models this speaker can ride, or None for unknown speakers (warn-only
    doctrine: Sarvam adds speakers faster than we ship — never reject those)."""
    info = SPEAKER_CATALOG.get((speaker or "").strip().lower())
    return info["models"] if info else None
