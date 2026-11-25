"""Caller calendar claims checked against the real calendar (call 1043)."""
from datetime import datetime

import pytest

from app.voice.date_claims import check_date_claims
from app.voice.prompt_blocks import date_claim_block

NOW = datetime(2026, 10, 9, 15, 30)          # a Friday


def _kinds(text):
    return [(i.kind, i.said) for i in check_date_claims(text, NOW)]


def test_call_1043_sunday_is_not_the_12th():
    assert _kinds("संडे को तो बारह अक्टूबर है।") == [
        ("weekday_mismatch", "Sunday is 12 October")]
    block = "\n".join(date_claim_block("संडे को तो बारह अक्टूबर है।", NOW))
    assert "12 October is a Monday" in block
    assert "Sunday 11 October" in block
    assert "NEVER change what today is" in block


@pytest.mark.parametrize("text,said", [
    ("Sunday is 12th October right?", "Sunday is 12 October"),
    ("आज 12 तारीख है", "today is 12 October"),
    ("aaj sunday hai", "today is Sunday"),
])
def test_contradictions_are_caught(text, said):
    assert [s for _, s in _kinds(text)] == [said]


@pytest.mark.parametrize("text", [
    "नहीं, आप एक काम कीजिए, मेरी 14th ऑक्टोबर।",
    "संडे यानी 11 अक्टूबर ठीक है",
    "Wednesday 14th October works",
    "today is Friday the 9th",
    "आज नहीं, संडे को आऊँगा",
    "aaj nahi kal 12 tarikh ko",
    "रविवार को दो बजे आ जाऊँगा",
    "दो बजे संडे को",
    "",
])
def test_true_or_unrelated_lines_are_silent(text):
    assert check_date_claims(text, NOW) == []
    assert date_claim_block(text, NOW) == []
