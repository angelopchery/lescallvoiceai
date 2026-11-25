"""Figures spoken in English inside a Hindi line (RUNBOOK §82).

Owner, 2026-10-15: "55 lakh is spoken out as paanch paanch lakh … I just
want the numbers to be spoken in English, fifty five lakhs; decimal → 'point';
nothing for '-'; the language otherwise unchanged." Opt-in per agent via
`languages.tts_numbers_english`; the transcript keeps the digits.
"""

from __future__ import annotations

import types

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import pipeline as pl
from app.voice.spoken_numbers import (english_numbers, int_words,
                                      ordinal_words, year_words)


# ── the lines of call 918076562864_20261015_055112, as Bulbul will now hear them

@pytest.mark.parametrize("line, spoken", [
    ("जहाँ 2 BHK और 3 BHK ऑप्शंस ₹50-55 लाख से शुरू होते हैं।",
     "जहाँ two BHK और three BHK ऑप्शंस fifty to fifty five लाख से शुरू होते हैं।"),
    ("4 BHK bungalow की कीमत ₹2.15 Cr से शुरू होती है और 5 BHK की ₹2.35 Cr से ₹3.70 Cr तक है।",
     "four BHK bungalow की कीमत two point one five crore से शुरू होती है और "
     "five BHK की two point three five crore से three point seven crore तक है।"),
    ("3 BHK apartments करीब 78 से 85 लाख से शुरू होते हैं",
     "three BHK apartments करीब seventy eight से eighty five लाख से शुरू होते हैं"),
    ("₹50–55 लाख", "fifty to fifty five लाख"),           # en-dash range too
    ("about ₹1.08–1.11 Cr (4 BHK)", "about one point zero eight to one point one one crore (four BHK)"),
])
def test_the_pramukh_price_lines(line, spoken):
    assert english_numbers(line) == spoken


# ── each rule on its own ──────────────────────────────────────────────────

@pytest.mark.parametrize("line, spoken", [
    ("1.5 BHK", "one point five BHK"),
    ("3,890 sq ft", "three thousand eight hundred ninety sq ft"),
    ("5,00,000", "five lakh"),
    ("₹35,000", "thirty five thousand rupees"),
    ("₹ 90 लाख", "ninety लाख"),
    ("2 करोड़", "two करोड़"),
    ("डेढ़ करोड़", "डेढ़ करोड़"),                       # number WORDS untouched
    ("दो मिनट बात करना ठीक रहेगा?", "दो मिनट बात करना ठीक रहेगा?"),
    ("५५ लाख", "fifty five लाख"),                       # Devanagari digits
    ("4-BHK", "four BHK"),
    ("5%", "five percent"), ("12.5 %", "twelve point five percent"),
    ("15th October", "fifteenth October"), ("1st", "first"),
    ("22nd", "twenty second"), ("3rd", "third"), ("20th", "twentieth"),
    ("10:30 बजे", "ten thirty बजे"), ("9:05 am", "nine oh five am"),
    ("11:00 बजे", "eleven बजे"),
    ("+91 87583 70000", "nine one eight seven five eight three seven zero zero zero zero"),
    ("9876543210", "nine eight seven six five four three two one zero"),
    ("2026 में", "twenty twenty six में"),
    ("1995 में", "nineteen ninety five में"),
    ("2000 sq ft", "two thousand sq ft"),              # a unit: not a year
    ("₹2000", "two thousand rupees"),
    ("2 से 3 साल", "two से three साल"),
    ("Pramukh Swarit", "Pramukh Swarit"),
    ("", ""),
])
def test_each_rule(line, spoken):
    assert english_numbers(line) == spoken


def test_a_line_without_digits_is_returned_as_is():
    s = "आपने कुछ समय पहले हमारे किसी project में interest दिखाया था"
    assert english_numbers(s) is s


@pytest.mark.parametrize("n, words", [
    (0, "zero"), (7, "seven"), (15, "fifteen"), (20, "twenty"),
    (55, "fifty five"), (100, "one hundred"), (215, "two hundred fifteen"),
    (1000, "one thousand"), (3890, "three thousand eight hundred ninety"),
    (100000, "one lakh"), (350000, "three lakh fifty thousand"),
    (10000000, "one crore"),
    (23500000, "two crore thirty five lakh"),
    (123456789, "twelve crore thirty four lakh fifty six thousand seven hundred eighty nine"),
])
def test_indian_grouping(n, words):
    assert int_words(n) == words


def test_years_and_ordinals():
    assert year_words(2026) == "twenty twenty six"
    assert year_words(2009) == "two thousand nine"
    assert year_words(1900) == "nineteen hundred"
    assert year_words(2005) == "two thousand five"
    assert ordinal_words(1) == "first"
    assert ordinal_words(9) == "ninth"
    assert ordinal_words(12) == "twelfth"
    assert ordinal_words(21) == "twenty first"
    assert ordinal_words(30) == "thirtieth"
    assert ordinal_words(100) == "one hundredth"


# ── the agent flag and the pipeline hook ──────────────────────────────────

def _def(**languages):
    return AgentDefinition.from_dict({
        "agent_id": "t_82", "version": "1.0.0",
        "identity": {"agent_name": "Akshay", "company_name": "Pramukh Swarit"},
        "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {},
                      **languages},
        "call_flow": {"stages": []},
        "slots": [],
        "response_bank": {"opening_greeting": "hi"},
    })


def test_the_flag_is_off_unless_the_agent_opts_in():
    assert _def().languages.tts_numbers_english is False
    assert _def(tts_numbers_english=True).languages.tts_numbers_english is True
    assert _def(tts_numbers_english="true").languages.tts_numbers_english is True


def test_the_pipeline_converts_only_for_an_opted_in_agent():
    line = "कीमत ₹50-55 लाख से शुरू होती है"
    assert pl._spoken_numbers_text(line, False) == line
    assert pl._spoken_numbers_text(line, True) == \
        "कीमत fifty to fifty five लाख से शुरू होती है"
    assert pl._spoken_numbers_text("", True) == ""


def test_the_pipeline_reads_the_flag_from_the_live_agent():
    on = types.SimpleNamespace(conversation=types.SimpleNamespace(_def=_def(tts_numbers_english=True)))
    off = types.SimpleNamespace(conversation=types.SimpleNamespace(_def=_def()))
    none = types.SimpleNamespace(conversation=None)
    assert pl.VoicePipeline._tts_numbers_english(on) is True
    assert pl.VoicePipeline._tts_numbers_english(off) is False
    assert pl.VoicePipeline._tts_numbers_english(none) is False


def test_the_range_is_converted_before_the_dash_rewrite():
    """The player rewrites "–" to ", " for Bulbul; a "50–55" range must be
    words by then, or it is spoken "fifty, fifty five"."""
    text = pl._spoken_numbers_text("₹50–55 लाख — शानदार", True)
    text = text.replace("—", ", ").replace("–", ", ")
    assert text == "fifty to fifty five लाख ,  शानदार".replace(" ,  ", " , ") \
        or "fifty to fifty five" in text
    assert "fifty, fifty" not in text
