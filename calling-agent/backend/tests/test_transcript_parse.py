"""dashboard/server.py::_read_transcript — the transcript view's only parser.

Regression cover for the 2026-08-04 outage: call_audit.py started appending a
live-VAD span to CALLER turn headers ("[00:10.545] [spoke 7.26-9.98s]", commit
bacbd82) while the parser's timestamp pattern anchored "$" immediately after
the stamp. Annotated headers stopped being recognised as headers, so the whole
block fell through as body text and find(":") split inside the timestamp — the
turn was filed under a speaker literally named "[00". Between 76% and 99% of
caller turns per call disappeared from the transcript view and from the AI
analysis, on every tenant, while the recording on disk was perfectly intact.

The agent side was untouched (only caller events carry a span), which is why
the calls still read as if the agent had heard and answered correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (str(_REPO / "dashboard"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture()
def read_transcript(tmp_path, monkeypatch):
    """_read_transcript bound to a temp recordings root."""
    import server

    monkeypatch.setattr(server, "RECORDINGS_DIR", tmp_path)

    def _write(folder: str, body: str):
        d = tmp_path / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / "transcript.txt").write_text(body, encoding="utf-8")
        return server._read_transcript(folder)

    return _write


# The exact shape call_audit.py writes today: agent turns bare, caller turns
# carrying the live-VAD span.
SPANNED = """[00:00.004]
Agent: Hello, am I speaking with Saptaswa?

[00:07.638]
Caller: hello

[00:10.545] [spoke 7.26-9.98s]
Caller: yes, i believe we have talked before.

[00:11.952]
Agent: Yes, we have! Is now a good time?

[00:18.911] [spoke 17.42-18.48s]
Caller: happy.
"""


def test_spanned_caller_turns_are_attributed_to_the_caller(read_transcript):
    msgs = read_transcript("919830263578_20260806_065055", SPANNED)

    assert [m["speaker"] for m in msgs] == [
        "Agent", "Caller", "Caller", "Agent", "Caller",
    ]
    # The regression signature: a speaker parsed out of the timestamp itself.
    assert not any(m["speaker"].startswith("[") for m in msgs)


def test_span_does_not_leak_into_the_spoken_text(read_transcript):
    msgs = read_transcript("f", SPANNED)
    spanned = msgs[2]

    assert spanned["text"] == "yes, i believe we have talked before."
    assert "spoke" not in spanned["text"]
    assert spanned["timestamp"] == "00:10.545"
    assert spanned["span"] == "7.26-9.98"


def test_unannotated_transcripts_still_parse(read_transcript):
    """Every call recorded before 2026-08-04 has bare headers — the fix must
    not trade one format for the other."""
    msgs = read_transcript("legacy", """[00:00.004]
Agent: Hello there.

[00:07.638]
Caller: hi
""")
    assert [(m["speaker"], m["text"]) for m in msgs] == [
        ("Agent", "Hello there."), ("Caller", "hi"),
    ]
    assert all(m["span"] == "" for m in msgs)


def test_future_bracket_annotations_do_not_break_attribution(read_transcript):
    """The header grammar is open-ended on purpose: the next annotation added
    upstream must not silently re-open this outage."""
    msgs = read_transcript("future", """[00:10.545] [spoke 7.26-9.98s] [conf 0.91]
Caller: still mine.
""")
    assert msgs[0]["speaker"] == "Caller"
    assert msgs[0]["text"] == "still mine."


def test_colon_inside_speech_is_kept_with_the_speech(read_transcript):
    msgs = read_transcript("colon", """[00:05.000] [spoke 1.00-4.00s]
Caller: meet me at 4:30 please
""")
    assert msgs[0]["speaker"] == "Caller"
    assert msgs[0]["text"] == "meet me at 4:30 please"


def test_headerless_block_never_invents_a_speaker(read_transcript):
    """A body line with no recognisable speaker prefix must land as Unknown
    rather than claiming whatever precedes the first colon."""
    msgs = read_transcript("junk", "12.345] [spoke 1.0-2.0s] orphaned text\n")
    assert msgs[0]["speaker"] == "Unknown"


# ---------------------------------------------------------------------------
# The SAME transcript format has a second parser: shared.intelligence.embed
# .parse_turns, feeding the Intelligence call view, the embeddings and semantic
# search. It is a finditer, so an unmatched header is not an error — the turn is
# silently deleted. bacbd82 cost it the same 76-99% of caller turns per call.
# ---------------------------------------------------------------------------

def _parse_turns(text: str):
    from shared.intelligence.embed import parse_turns
    return parse_turns(text)


def test_intelligence_keeps_spanned_caller_turns():
    turns = _parse_turns(SPANNED)
    assert [s for _, s, _ in turns] == [
        "Agent", "Caller", "Caller", "Agent", "Caller",
    ]


def test_intelligence_drops_nothing_and_keeps_text_clean():
    turns = _parse_turns(SPANNED)
    assert len(turns) == 5
    ts, speaker, text = turns[2]
    assert (ts, speaker) == ("00:10.545", "Caller")
    assert text == "yes, i believe we have talked before."


def test_intelligence_still_reads_legacy_transcripts():
    turns = _parse_turns("[00:00.004]\nAgent: Hello there.\n\n[00:07.638]\nCaller: hi\n")
    assert [(s, t) for _, s, t in turns] == [
        ("Agent", "Hello there."), ("Caller", "hi"),
    ]


def test_intelligence_tolerates_future_annotations_and_long_calls():
    turns = _parse_turns(
        "[103:20.500] [spoke 1.0-2.0s] [conf 0.9]\nCaller: past the hundredth minute.\n")
    assert len(turns) == 1
    assert turns[0][1] == "Caller"
    assert turns[0][2] == "past the hundredth minute."
