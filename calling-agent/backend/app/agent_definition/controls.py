"""
controls — "is this a real setting on this platform, or did something just
invent it?"

Born from the 2026-08-04 forensics. `AgentDefinition.from_dict` builds every
field by explicit reads, so an unknown key is silently DROPPED; and the
store's scoping proof (`write_agent(expected_paths=…)`) only asserts that
nothing changed OUTSIDE the declared scope — it never asserts that anything
INSIDE it changed. A write to a key the schema does not know therefore
produces an empty diff, bumps the revision, and is reported as a successful
update. Nothing on any call is different, and the user finds out on the
phone.

The untyped free-dict subtrees (`call_flow.connect`, `guardrails.honesty`,
`follow_up.callback`, …) are the sharp edge: `from_dict` passes them through
verbatim, so a hallucinated key there PERSISTS to the YAML and still changes
nothing.

Shared module on purpose — the Copilot, the Builder API and the fleet lint
must all agree on what a real control is.
"""

from __future__ import annotations

import dataclasses
import difflib
import typing

from .schema import AgentDefinition

# ── free-dict subtrees: hand-curated allowlists ─────────────────────────────
# These fields are typed `dict` in the schema, so from_dict cannot police
# them. Each entry is sourced from the schema's own comment block above the
# field — keep them in sync; test_copilot_control_surface.py fails if a key
# any LIVE agent already carries is missing here.
KNOWN_LEAF_KEYS: dict[str, frozenset[str]] = {
    "call_flow.connect": frozenset({
        "hello_fastpath", "delivery_guard", "intro_line"}),
    "call_flow.termination": frozenset({
        "llm_end_call", "confirm_before_end", "end_conditions",
        "farewell_patterns", "silence_hangup", "max_call_minutes",
        # RUNBOOK §75: the owner's "anything else before I go?" line
        # ({lang: str}), asked before every goodbye.
        "pre_close_question"}),
    # RUNBOOK §78: per-agent turn-taking (pause hold, back-channel guard,
    # deferred-barge override, tail cut).
    "call_flow.turn_taking": frozenset({
        "pause_hold_ms", "max_merged_fragments", "backchannel_guard",
        "sustained_barge_ms", "tail_barge_in"}),
    "call_flow.completed_when": frozenset({
        "required_slots", "require_appointment", "all_collect_filled"}),
    "call_flow.scheduling": frozenset({
        "style", "offer_count", "enabled", "lead_time_minutes",
        "working_hours", "slot_granularity_minutes", "days", "window"}),
    "call_flow.callback_policy": frozenset({
        "enabled", "capture_slot", "responses", "counts_as_decline",
        "max_offers", "capture_exact_time"}),
    "knowledge_base.pricing_policy": frozenset({
        "mode", "disclose", "responses", "escalate_to", "defer_line",
        "topics", "second_ask_approx", "approx_line"}),
    "knowledge_base.fact_policy": frozenset({
        "mode", "unknown_response", "allow_inference", "defer_line",
        "topics"}),
    "knowledge_base.product_lock": frozenset({
        "enabled", "lock_after", "scope"}),
    # The ONE catalogue item pitched proactively (RUNBOOK §55). `mode` is
    # exclusive|lead; `reason` is the clause the MATCH RULES band quotes.
    "knowledge_base.primary_project": frozenset({
        "id", "mode", "reason", "budget_switch"}),
    "guardrails.honesty": frozenset({
        "never_claim_human", "disclose_ai_when_asked", "responses",
        "unknown_answer_policy", "knowledge_grounding",
        "forbidden_fabrications"}),
    "guardrails.compliance": frozenset({
        "dnd_honour", "recording_disclosure", "responses",
        "disclaimers", "consent_line", "restricted_topics"}),
    # `action` and `handoff_message` are READ by prompt_blocks.firm_rules_block
    # (the ━━ ESCALATION ━━ band) and documented on GuardrailsConfig; they were
    # missing here, so the first agent to use the band failed the live-agent
    # fence (Prakhar, 2026-09-24).
    "guardrails.escalation": frozenset({
        "enabled", "triggers", "responses", "transfer_to", "action",
        "handoff_message"}),
    "follow_up.whatsapp_promise": frozenset({
        "enabled", "responses", "capture_slot", "assets", "promise_line",
        "track_projects", "offer_from_roles"}),
    # `default_delay_hours` is the spelling the Copilot is TOLD to write
    # (agent_copilot_api build prompt) and FollowUpConfig documents; nothing
    # at runtime reads either spelling yet, but a key the builder emits must
    # not read as a typo.
    "follow_up.callback": frozenset({
        "enabled", "responses", "capture_slot", "default_delay_minutes",
        "default_delay_hours"}),
    "follow_up.clarify_commitments": frozenset({
        "enabled", "responses", "max_asks"}),
    "follow_up.auto_execute": frozenset({
        "enabled", "mode", "max_attempts"}),
    "post_call.success_definition": frozenset({
        "required_slots", "require_appointment", "intents"}),
    # criteria_prompt / score_labels are what shared/intelligence/extract.py
    # and the dashboard actually read (and what the Copilot's own prompt
    # tells the model to write); "weights"/"thresholds" are read by nothing.
    "post_call.scoring": frozenset({
        "enabled", "auto_run", "criteria_prompt", "score_labels",
        "weights", "thresholds"}),
    "post_call.lead_profile": frozenset({
        "fields", "summary_template"}),
}

_SCHEMA_PATHS: frozenset[str] | None = None


def _is_open_mapping(tp) -> bool:
    """Fields whose interior the schema does not type are open by design:
    `dict[str, X]` (a `hi` key is as legitimate as `en`), a bare `dict`
    (call_flow.stages[].advance_when), and `list[dict]` whose elements are
    free-form (knowledge_base.catalog items). Refusing inside these would
    edit-lock agents that are working today."""
    if tp is dict or tp is list:
        return True
    origin = typing.get_origin(tp)
    if origin in (dict, typing.Dict):
        return True
    if origin in (list, typing.List, tuple):
        args = typing.get_args(tp)
        return not args or not any(dataclasses.is_dataclass(a) for a in args)
    return False


def _walk(cls, prefix: str, out: set[str]) -> None:
    try:
        fields = dataclasses.fields(cls)
    except TypeError:
        return
    hints = typing.get_type_hints(cls)
    for f in fields:
        path = f"{prefix}{f.name}"
        out.add(path)
        tp = hints.get(f.name, f.type)
        if dataclasses.is_dataclass(tp):
            _walk(tp, path + ".", out)
        else:
            # list[SomeDataclass] — the element's fields are real paths too
            args = typing.get_args(tp)
            for a in args:
                if dataclasses.is_dataclass(a):
                    _walk(a, path + ".", out)


def schema_paths() -> frozenset[str]:
    """Every dotted path the schema knows, derived from the dataclasses."""
    global _SCHEMA_PATHS
    if _SCHEMA_PATHS is None:
        acc: set[str] = set()
        _walk(AgentDefinition, "", acc)
        _SCHEMA_PATHS = frozenset(acc)
    return _SCHEMA_PATHS


def _open_paths() -> frozenset[str]:
    """Paths whose value is a dict[str, X] — open by design."""
    acc: set[str] = set()

    def _rec(cls, prefix: str) -> None:
        try:
            fields = dataclasses.fields(cls)
        except TypeError:
            return
        hints = typing.get_type_hints(cls)
        for f in fields:
            path = f"{prefix}{f.name}"
            tp = hints.get(f.name, f.type)
            if dataclasses.is_dataclass(tp):
                _rec(tp, path + ".")
            else:
                if _is_open_mapping(tp) and path not in KNOWN_LEAF_KEYS:
                    acc.add(path)
                for a in typing.get_args(tp):
                    if dataclasses.is_dataclass(a):
                        _rec(a, path + ".")

    _rec(AgentDefinition, "")
    return frozenset(acc)


_OPEN_PATHS: frozenset[str] | None = None


def open_paths() -> frozenset[str]:
    global _OPEN_PATHS
    if _OPEN_PATHS is None:
        _OPEN_PATHS = _open_paths()
    return _OPEN_PATHS


def _strip_indices(path: str) -> str:
    out = []
    for part in str(path).split("."):
        out.append(part.split("[", 1)[0])
    return ".".join(p for p in out if p)


def unknown_paths(paths) -> list[tuple[str, str]]:
    """[(path, reason)] for every path that names no real control.

    Conservative by design: anything under an open mapping (language maps,
    per-stage dicts) is exempt, and a path is only refused when its own
    subtree is one we can vouch for.
    """
    known = schema_paths()
    opens = open_paths()
    bad: list[tuple[str, str]] = []
    for raw in (str(p) for p in (paths or ())):
        path = _strip_indices(raw)
        if not path or path in known:
            continue
        # Under an open mapping (response_bank.objection_responses.ta, …)?
        if any(path == o or path.startswith(o + ".") for o in opens):
            continue
        # Inside a curated free-dict subtree: the FIRST segment below it must
        # be a key we know; anything deeper belongs to that key's own shape.
        subtree = next((k for k in KNOWN_LEAF_KEYS
                        if path.startswith(k + ".")), None)
        if subtree:
            leaf = path[len(subtree) + 1:].split(".", 1)[0]
            if leaf in KNOWN_LEAF_KEYS[subtree]:
                continue
            bad.append((raw, f"'{subtree}' has no setting called '{leaf}'"))
            continue
        # A path whose parent IS a known dataclass field but which is not
        # itself known — e.g. call_flow.whisper_mode.
        parent = path.rsplit(".", 1)[0] if "." in path else ""
        if not parent or parent in known:
            bad.append((raw, f"'{path}' is not a setting this platform has"))
    return bad


def nearest_controls(topic: str, k: int = 3) -> list[str]:
    """Real control paths closest to `topic` — so a refusal can name what
    the platform CAN do instead of only what it cannot."""
    pool = set(schema_paths())
    for sub, leaves in KNOWN_LEAF_KEYS.items():
        pool.update(f"{sub}.{leaf}" for leaf in leaves)
    needle = _strip_indices(topic).lower()
    hits = difflib.get_close_matches(needle, sorted(pool), n=k, cutoff=0.45)
    if hits:
        return hits
    tail = needle.rsplit(".", 1)[-1]
    return sorted(p for p in pool if tail and tail in p.lower())[:k]
