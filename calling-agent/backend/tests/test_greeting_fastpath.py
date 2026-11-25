"""A bare "hello" must not pay the merge hold (2026-08-06).

The transcript merge window reassembles an utterance ASR split in two, and a
short transcript arriving while the caller has only just gone quiet is treated
as the head of such a split (MERGE_NO_FASTPATH_MAX_WORDS/_QUIET_MS).

A bare greeting is the one case where that is wrong: it is a COMPLETE turn —
the caller checking the line is still alive — so holding it adds exactly the
silence that made them say it. Measured on the stored corpus, the first caller
turn waited a median 1.36s and a p90 of 4.82s before the agent's audio was even
queued.

Everything that is genuinely a fragment must keep the full hold.
"""

from __future__ import annotations

import pytest

from app.voice.pipeline import _is_self_contained_greeting


@pytest.mark.parametrize("text", [
    "hello", "Hello.", "hi", "hey", "HELLO?", "hallo", "helo",
    "हेलो", "हैलो", "नमस्ते", "namaste", "kem cho", "હેલો",
    "hello hello", "hello hello hello",
])
def test_bare_greetings_take_the_fast_path(text):
    assert _is_self_contained_greeting(text)


@pytest.mark.parametrize("text", [
    "hello I wanted to ask",          # greeting + real content
    "yes",                            # affirmation, may head a longer answer
    "haan",
    "मैं",
    "hello my name is Harshal",
    "one moment",
    "",
    "   ",
    "hello hello hello hello",        # 4 tokens — past the short-turn bound
])
def test_everything_else_keeps_the_full_hold(text):
    assert not _is_self_contained_greeting(text)


def test_greeting_plus_content_is_never_fast_pathed():
    """The whole point of the merge window: 'hello' followed by the caller's
    actual sentence must still be allowed to reassemble."""
    assert not _is_self_contained_greeting("hello main Harshal bol raha hoon")
