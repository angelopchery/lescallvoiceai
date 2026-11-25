"""The voice-prompt correction pipeline (dashboard/copilot_voice.py).

A corrector that fixes nine mishearings and mangles one ordinary sentence is
worse than no corrector at all: the mangled one is silent, and the user sends
it to the copilot believing they said something else. So the bar here is
asymmetric — the repair tests say what it MUST fix, and the much longer
leave-alone battery says what it must never touch.
"""

from __future__ import annotations

import asyncio
import io
import struct
import sys
import wave

import pytest

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "dashboard"))
import copilot_voice as cv                                       # noqa: E402


TENANT = ["hardik-2-inbound", "Pooja", "B2 Market", "Motilal Oswal",
          "pramukh", "Vishal Patel", "Pramukh Group", "shubh", "Ahmedabad"]


@pytest.fixture()
def lex():
    return cv.Lexicon(TENANT + list(cv._PRODUCT_LEXICON))


def fix(text: str, lex) -> str:
    return cv.tidy(cv.repair_vocabulary(text, lex)[0])


# ---------------------------------------------------------------------------
# What it must fix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("said,want", [
    # The whole reason this exists: a slug spoken aloud is heard as words.
    ("make hardik 2 inbound greet in gujrati", "hardik-2-inbound"),
    ("open hardik 2 inbound", "hardik-2-inbound"),
    # Verbatim from Sarvam on a real recording of that first prompt — the
    # digit came back as the word "to".
    ("Make hardik to inbound greet in Gujarati.", "hardik-2-inbound"),
    # Proper nouns the recognizer has never seen.
    ("who is the agent for motilal osval", "Motilal Oswal"),
    ("change poojas opening line", "Pooja"),
    ("switch the voice to shub", "shubh"),
    ("book a visit in ahmadabad", "Ahmedabad"),
    # Platform jargon split, joined or mis-spelled.
    ("set the out bound greeting", "outbound"),
    ("i want a whats app follow up", "WhatsApp"),
    ("what is barge in", "barge-in"),
    ("make the agent speak gujrati", "Gujarati"),
])
def test_repairs_what_stt_actually_gets_wrong(said, want, lex):
    assert want in fix(said, lex)


# ---------------------------------------------------------------------------
# What it must NEVER touch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "make it super short and to the point",
    "the agent should never quote a price",
    "show me the analytics for last week",
    "add a slot for email and phone number",
    "can you make the opening a bit warmer",
    "it should ask for their budget before booking",
    "put it on the second number instead",
    "how many calls did we take yesterday",
    "the tone is too formal for our customers",
    "stop asking for the name twice",
    "i think the greeting is fine as it is",
    "we need this live by monday morning",
])
def test_leaves_ordinary_english_alone(said, lex):
    _, fixes = cv.repair_vocabulary(said, lex)
    assert fixes == [], f"rewrote an ordinary sentence: {fixes}"


@pytest.mark.parametrize("said", [
    # "greet in" concatenates to "greetin", which scores 93 against
    # "greeting". A window that ends on a preposition was cut in the wrong
    # place — it is never a misheard name.
    "i want to greet in hindi",
    "change the greeting in the morning",
    "ask them to call in the evening",
    "tell it to hand off to a human",
])
def test_a_window_cut_on_a_function_word_is_not_a_name(said, lex):
    out, fixes = cv.repair_vocabulary(said, lex)
    assert not [f for f in fixes if " " in f["from"]], fixes


def test_plurals_of_jargon_survive(lex):
    """"agents" is the plural the speaker meant, not a mishearing of "agent"."""
    out, fixes = cv.repair_vocabulary("show me all my agents and their slots", lex)
    assert "agents" in out and "slots" in out, out
    assert fixes == []


def test_a_word_that_merely_scores_well_is_not_a_mishearing(lex):
    """The failure the call pipeline's post-processor documents: a high fuzzy
    score is not evidence when the word is ordinary English."""
    out, _ = cv.repair_vocabulary("keep the reply short and the tone light", lex)
    assert out == "keep the reply short and the tone light"


def test_an_empty_lexicon_is_a_no_op():
    text = "make hardik 2 inbound greet in gujrati"
    assert cv.repair_vocabulary(text, cv.Lexicon([])) == (text, [])


# ---------------------------------------------------------------------------
# Tidy
# ---------------------------------------------------------------------------

def test_tidy_removes_fillers_and_stutters():
    assert cv.tidy("um can you uh make the the agent speak hindi") == \
        "Can you make the agent speak hindi?"


def test_tidy_keeps_meant_repetition():
    assert "no no" in cv.tidy("no no that is wrong").lower()


def test_tidy_punctuates_questions_and_statements():
    assert cv.tidy("what is the greeting").endswith("?")
    assert cv.tidy("change the greeting").endswith(".")
    assert cv.tidy("i want two agents").startswith("I want")


def test_tidy_of_nothing_is_nothing():
    assert cv.tidy("   um  uh ") == ""


def test_sentence_casing_never_touches_a_slug():
    """agents/hardik-2-inbound is a directory; agents/Hardik-2-inbound is not.
    Seen for real on the Hindi round trip, where the slug lands first."""
    assert cv.tidy("hardik-2-inbound ka greeting chhota karo") == \
        "hardik-2-inbound ka greeting chhota karo."
    assert cv.tidy("aap kaise ho").startswith("Aap")


# ---------------------------------------------------------------------------
# Romanization — Hindi in, Hinglish out
# ---------------------------------------------------------------------------

class _Transliterator:
    """Stands in for Sarvam's /transliterate."""

    def __init__(self, reply="Aap kaise ho, sab thik", status=200, delay=0.0):
        self.reply, self.status, self.delay = reply, status, delay
        self.calls: list[dict] = []

    async def post(self, url, headers=None, json=None):
        self.calls.append(json or {})
        if self.delay:
            await asyncio.sleep(self.delay)
        payload, status = {"transliterated_text": self.reply}, self.status

        class _R:
            status_code = status
            text = str(payload)
            @staticmethod
            def json():
                return payload
        return _R()


def test_devanagari_becomes_hinglish():
    got = asyncio.run(cv.romanize("आप कैसे हो, सब ठीक", "hi-IN",
                                  client=_Transliterator()))
    assert got == "Aap kaise ho, sab thik"


def test_english_never_pays_for_a_transliteration_call():
    client = _Transliterator()
    got = asyncio.run(cv.romanize("make the greeting shorter", "en-IN", client=client))
    assert got == "make the greeting shorter"
    assert client.calls == [], "called the API for a Latin-script prompt"


def test_the_detected_language_is_passed_through_when_supported():
    client = _Transliterator()
    asyncio.run(cv.romanize("આપ કેમ છો", "gu-IN", client=client))
    assert client.calls[0]["source_language_code"] == "gu-IN"
    assert client.calls[0]["target_language_code"] == "en-IN"


def test_an_unsupported_language_falls_back_to_auto_detection():
    client = _Transliterator()
    asyncio.run(cv.romanize("आप कैसे हो", "xx-XX", client=client))
    assert client.calls[0]["source_language_code"] == "auto"


def test_digits_stay_digits():
    """spoken_form would turn "2" into "do" and break the slug repair that
    runs on the very next line."""
    client = _Transliterator()
    asyncio.run(cv.romanize("हार्दिक 2 इनबाउंड", "hi-IN", client=client))
    assert client.calls[0]["spoken_form"] is False
    assert client.calls[0]["numerals_format"] == "international"


@pytest.mark.parametrize("client", [
    _Transliterator(status=502),
    _Transliterator(reply=""),
    _Transliterator(reply="आप कैसे हो"),      # handed back the same script
])
def test_a_failed_transliteration_keeps_the_original(client):
    said = "आप कैसे हो"
    assert asyncio.run(cv.romanize(said, "hi-IN", client=client)) == said


def test_a_slow_transliteration_keeps_the_original(monkeypatch):
    monkeypatch.setattr(cv, "TRANSLITERATE_TIMEOUT_S", 0.05)
    said = "आप कैसे हो"
    got = asyncio.run(cv.romanize(said, "hi-IN", client=_Transliterator(delay=5)))
    assert got == said


def test_hinglish_questions_get_a_question_mark():
    assert cv.tidy("aap kaise ho sab thik") == "Aap kaise ho sab thik?"
    assert cv.tidy("kem cho") == "Kem cho?"
    # …and a Hinglish statement still gets a full stop.
    assert cv.tidy("greeting chhota karo").endswith(".")


def test_romanized_text_still_gets_the_vocabulary_repair(lex):
    """The stages are ordered romanize → repair for exactly this reason: the
    lexicon is Latin-script, so it can only fix what has already been
    romanized."""
    out, fixes = cv.repair_vocabulary("hardik 2 inbound ka greeting badlo", lex)
    assert "hardik-2-inbound" in out


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def _wav(seconds: float = 0.5, sr: int = 16_000, channels: int = 1) -> bytes:
    frames = int(sr * seconds)
    pcm = b"".join(struct.pack("<h", (i % 800) - 400) * channels
                   for i in range(frames))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


def test_browser_wav_decodes_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(cv, "_decode_ffmpeg",
                        lambda *_: pytest.fail("took the ffmpeg path"))
    pcm, sr = cv.decode_audio(_wav(0.25))
    assert sr == 16_000 and len(pcm) == 4000 * 2


def test_stereo_is_folded_to_mono():
    pcm, sr = cv.decode_audio(_wav(0.25, channels=2))
    assert sr == 16_000 and len(pcm) == 4000 * 2


def test_a_recording_that_is_all_silence_returns_nothing(lex):
    out = asyncio.run(cv.transcribe(_wav(0.05), lex))
    assert out["empty"] is True and out["text"] == ""


def test_an_over_long_recording_is_refused(lex, monkeypatch):
    monkeypatch.setattr(cv, "MAX_AUDIO_SECONDS", 0.1)
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(cv.transcribe(_wav(0.5), lex))


# ---------------------------------------------------------------------------
# Polish — must never be able to make things worse or slower
# ---------------------------------------------------------------------------

class _SlowClient:
    def __init__(self, delay=5.0, reply="whatever"):
        async def _create(**_kw):
            await asyncio.sleep(delay)
            return type("R", (), {"choices": [type("C", (), {
                "message": type("M", (), {"content": reply})()})()]})()
        self.chat = type("Chat", (), {"completions": type(
            "Comp", (), {"create": staticmethod(_create)})()})()


def test_polish_timeout_keeps_the_fast_text(monkeypatch):
    monkeypatch.setattr(cv, "POLISH_TIMEOUT_S", 0.05)
    text = "Make the greeting shorter please."
    got = asyncio.run(cv.polish(text, lambda: _SlowClient(), "m"))
    assert got == text


def test_polish_that_answers_instead_of_correcting_is_discarded():
    text = "Make the greeting shorter please."
    answer = ("Sure! Here is how you can shorten a greeting: first, open the "
              "agent editor, then find the opening line field and " * 2)
    got = asyncio.run(cv.polish(text, lambda: _SlowClient(0, answer), "m"))
    assert got == text


def test_polish_failure_keeps_the_fast_text():
    class _Broken:
        chat = type("C", (), {"completions": type("X", (), {
            "create": staticmethod(lambda **_: (_ for _ in ()).throw(
                RuntimeError("502")))})()})()
    text = "Make the greeting shorter please."
    assert asyncio.run(cv.polish(text, lambda: _Broken(), "m")) == text


def test_polish_leaves_very_short_prompts_alone():
    assert asyncio.run(cv.polish("hi", lambda: _SlowClient(0, "Hello!"), "m")) == "hi"
