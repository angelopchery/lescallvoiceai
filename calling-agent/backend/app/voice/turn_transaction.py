"""Turn transactions — snapshot/rollback for the continuation-merge feature.

A slow caller pauses mid-utterance; the pipeline endpoints fast and the agent
answers the partial input. If the caller then resumes within the continuation
window, the pipeline needs to supersede that provisional turn: remove the
user+assistant pair from memory, restore every piece of router state the turn
mutated, and re-run one merged utterance.

ConversationRouter.begin_turn_transaction() calls capture() at the top of
process_stream (before any mutation); rollback_pending_turn() calls restore().
Snapshots are single-use and cheap (shallow dict/list/set copies of scalars,
plus a few small deepcopies) — well under a millisecond, never on the audio
path.

MAINTENANCE: any NEW per-turn-mutable state added to ConversationRouter,
ConversationState, ConversationMemory, or ConversationStateManager must be
added to the capture surface below, or a rollback will leak it. The
full-surface test in tests/test_continuation_merge.py catches drift.
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Router attributes restored verbatim (immutable values).
#
# Filler counters (_filler_counter/_fillers_played/_last_filler_turn) are
# deliberately NOT here: the filler AUDIO already played on the call and
# cannot be rolled back with the text state. Restoring the counters made the
# merged re-run pass _filler_allowed() again → a second filler back-to-back
# for what the caller experiences as one utterance, and played fillers
# stopped counting toward filler_max_per_call.
_ROUTER_SCALARS = (
    "last_agent_response",
    "_good_time_spoken",
    "last_agent_language",
    "_name_pending",
    "_name_confirmed",
    "_name_pending_turn",
    "_whatsapp_promised",
    "_callback_requested",
    "call_outcome",
    "_clarify_asked",
    "_suppress_time_match_turns",
    "_response_counter",
    "_last_nudge_stage",
    "_llm_failure_streak",
    "llm_fallback_turns",
    # RUNBOOK §73: identity re-confirmation + declined-visit memory.
    "_wrong_number_pending",
    "_identity_other",
    "_visit_declined_turn",
    "_visit_answer_first_turn",
)

# Router attributes holding flat containers (shallow copy suffices).
_ROUTER_CONTAINERS = (
    "_branch_takes",
    "_slot_confirm_turn",
    "llm_models_used",
)

# Router attributes holding nested structures (deepcopy).
_ROUTER_DEEP = (
    "_commitments",
    "_pending_clarify",
)


@dataclass
class TurnSnapshot:
    """Everything a single ConversationRouter turn can mutate."""

    user_text: str
    taken_at: float
    # ConversationMemory — a shallow COPY of the turns list (Turn objects are
    # never mutated in place), immune to _trim(); restoring it removes the
    # provisional user turn and any assistant turn in one move.
    turns: list
    booking: Any            # BookingData deepcopy
    call_state: Any         # CallState deepcopy
    context: Any            # ContextMemory deepcopy
    qa_asked: dict
    qa_answered: dict
    # ConversationStateManager runtime state.
    sm_state: dict
    sm_audit_len: int       # audit_log is append-only → truncate on restore
    sm_pending: set
    sm_asked_count: dict
    sm_asked_turn: dict
    sm_skipped: set
    sm_readback: set
    sm_extra_flags: dict
    sm_turn_index: int
    # ConversationState.__dict__ (covers dynamically-added attrs like
    # appointment_advanced).
    conv_state: dict
    # Router bookkeeping fields, grouped by copy discipline.
    router_scalars: dict
    router_containers: dict
    router_deep: dict
    deferred_bookings: list


def capture(router, user_text: str) -> TurnSnapshot:
    """Snapshot router state before the turn for `user_text` mutates it."""
    mem = router.memory
    sm = router.state_mgr
    return TurnSnapshot(
        user_text=user_text,
        taken_at=time.monotonic(),
        turns=list(mem.turns),
        booking=copy.deepcopy(mem.booking),
        call_state=copy.deepcopy(mem.call_state),
        context=copy.deepcopy(mem.context),
        qa_asked=dict(mem.qa_tracker._asked),
        qa_answered=dict(mem.qa_tracker._answered),
        sm_state=dict(sm.state),
        sm_audit_len=len(sm.audit_log),
        sm_pending=set(sm._pending_fields),
        sm_asked_count=dict(sm.asked_count),
        sm_asked_turn=dict(sm._asked_turn),
        sm_skipped=set(sm.skipped),
        sm_readback=set(sm.readback_done),
        sm_extra_flags=copy.deepcopy(sm.extra_flags),
        sm_turn_index=sm.turn_index,
        conv_state={
            k: (v.copy() if isinstance(v, (list, dict, set)) else v)
            for k, v in router.state.__dict__.items()
        },
        router_scalars={n: getattr(router, n) for n in _ROUTER_SCALARS},
        router_containers={
            n: copy.copy(getattr(router, n)) for n in _ROUTER_CONTAINERS
        },
        router_deep={n: copy.deepcopy(getattr(router, n)) for n in _ROUTER_DEEP},
        deferred_bookings=list(getattr(router, "_deferred_bookings", [])),
    )


def restore(router, snap: TurnSnapshot) -> None:
    """Rewind the router to `snap`. Snapshots are single-use — the caller
    clears its reference after restoring."""
    mem = router.memory
    sm = router.state_mgr

    mem.restore_turns(snap.turns)
    mem.booking = snap.booking
    mem.call_state = snap.call_state
    mem.context = snap.context
    mem.qa_tracker._asked = dict(snap.qa_asked)
    mem.qa_tracker._answered = dict(snap.qa_answered)

    # Keep the same dict identity — other components hold references to
    # sm.state (prompt builders read it live).
    sm.state.clear()
    sm.state.update(snap.sm_state)
    del sm.audit_log[snap.sm_audit_len:]
    sm._pending_fields = set(snap.sm_pending)
    sm.asked_count = dict(snap.sm_asked_count)
    sm._asked_turn = dict(snap.sm_asked_turn)
    sm.skipped = set(snap.sm_skipped)
    sm.readback_done = set(snap.sm_readback)
    sm.extra_flags = snap.sm_extra_flags
    sm.turn_index = snap.sm_turn_index

    # ConversationState: restore captured attrs, drop attrs added mid-turn
    # (e.g. appointment_advanced set via setattr during stage advance).
    for k in list(router.state.__dict__.keys()):
        if k not in snap.conv_state:
            delattr(router.state, k)
    for k, v in snap.conv_state.items():
        setattr(router.state, k, v)

    for n, v in snap.router_scalars.items():
        setattr(router, n, v)
    for n, v in snap.router_containers.items():
        setattr(router, n, v)
    for n, v in snap.router_deep.items():
        setattr(router, n, v)
    router._deferred_bookings = list(snap.deferred_bookings)

    logger.info(
        "[TURN-TXN] Rolled back provisional turn %r (audit truncated to %d, "
        "stage restored to %s)",
        snap.user_text[:50], snap.sm_audit_len,
        snap.conv_state.get("stage"),
    )
