"""The grounded-generation contract: how retrieved evidence is presented.

One definition, used by the live call path AND by the offline evaluation, so
an evaluation of faithfulness is an evaluation of the prompt that actually
ships. A second copy of this text inside the eval harness would be measuring a
prompt no caller ever hears.

THE VOICE CONSTRAINT
--------------------
The consumer of this context is a telephone agent. A conventional RAG prompt
asks the model to cite its sources inline, and here that produces a person on
the phone saying "according to source two, section three" — which no human
does, and which the platform's own knowledge-grounding block already forbids in
spirit. So the contract splits the two jobs a citation does:

  * attribution for the SYSTEM — every returned chunk is labelled [S1], [S2],
    and those labels flow into the structured `citations` on the turn record,
    the API response and the trace. Machine-readable, auditable, never spoken.
  * honesty for the CALLER — the model is told the evidence is its only source
    of business fact, and exactly what to say when the evidence does not cover
    the question.

The abstention line is phrased as an ACTION ("say you'll check with the team
and follow up"), not as a refusal ("say you don't know"). A refusal on a sales
call reads as incompetence; an offer to check reads as diligence, and this
platform has already shipped one regression where over-deflection cost real
conversations.
"""

from __future__ import annotations

from . import config

HEADER = "━━ DOCUMENT CONTEXT (RETRIEVED EVIDENCE) ━━"

# Kept short on purpose: it is re-sent on every turn of every call, and this
# platform measured prompt input at ~98% of the LLM bill.
_RULES_SPOKEN = (
    "▸ The numbered sources above are your ONLY evidence for facts about this "
    "business — prices, availability, specifications, policies, timelines.\n"
    "▸ If a detail is not written above, you do NOT know it: say you'll check "
    "with the team and follow up. Never guess, never fill a gap from general "
    "knowledge, never present an assumption as a fact.\n"
    # Without this the contract over-fires on multi-hop questions. Measured: a
    # caller asking whether EMI applied to a console was refused, while BOTH
    # facts needed — the console's price and the order value EMI starts at —
    # were sitting in the supplied evidence. Combining stated facts is not
    # guessing, and refusing to do it is its own failure.
    "▸ Facts stated in different sources may be COMBINED. If the sources give "
    "you the pieces, the conclusion is supported — say it. Only decline when a "
    "piece is genuinely missing.\n"
    "▸ Answer in your own words, in one or two spoken sentences. Do NOT read "
    "out the [S1] labels, do not say 'according to the document', and do not "
    "quote the text verbatim."
)

_RULES_TEXT = (
    "▸ The numbered sources above are your ONLY evidence for factual claims.\n"
    "▸ If the sources do not contain the answer, say so explicitly rather than "
    "answering from general knowledge.\n"
    "▸ Cite the sources you used as [S1], [S2] inline."
)

# Rendered INSTEAD of the evidence block when the retrieval gate abstained. The
# model is told there is no evidence rather than simply being given none —
# silence about a failed retrieval is what lets a model answer from its
# pretraining and sound just as confident.
NO_EVIDENCE_BLOCK = (
    "━━ DOCUMENT CONTEXT ━━\n"
    "No uploaded document covers this question — the search returned no "
    "supporting evidence.\n"
    "▸ Do NOT answer it from general knowledge. Say you'll check the exact "
    "details with the team and follow up, then continue toward your goal."
)


def render_context(sources: list[dict], spoken: bool | None = None) -> str:
    """Render the evidence block. `sources` are dicts with label/text/doc_type."""
    if not sources:
        return ""
    spoken = config.SPOKEN_CITATIONS is False if spoken is None else spoken
    lines = [HEADER]
    for src in sources:
        kind = f" ({src['doc_type']})" if src.get("doc_type") else ""
        lines.append(f"[{src['label']}]{kind} {(src.get('text') or '').strip()}")
    lines.append("")
    lines.append(_RULES_SPOKEN if spoken else _RULES_TEXT)
    return "\n".join(lines)


def eval_system_prompt(spoken: bool = False) -> str:
    """The system prompt the offline evaluation generates answers with.

    Not a simplified stand-in: it is the same contract text the live agent
    receives, with only the persona and call-flow layers of the real prompt
    removed, so faithfulness is attributable to the grounding rules rather than
    to a sales persona.
    """
    return (
        "You answer questions using ONLY the retrieved evidence you are given.\n"
        + (_RULES_SPOKEN if spoken else _RULES_TEXT)
        + "\nIf the evidence is insufficient, reply exactly: "
          "\"I don't have that in my sources.\" and nothing else."
    )
