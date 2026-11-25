"""A twin's call direction is its SLUG (RUNBOOK §60).

Regression source (2026-09-24, the Viraj family): Gemini sent
`call_flow.scheduling` as the JSON TEXT '{ "enabled": false }'. Nothing
decoded it, from_dict read {} for it, and the quality lints then called
.get() on the string — AFTER the write. create_agent wrote both twins and
answered "FAILED — DID NOT HAPPEN"; the retry was told "viraj-inbound already
exists — use update_agent", and the Copilot obediently wrote the outbound
script, call_direction included, onto the INBOUND twin. The Agents card then
drew two `outbound` rows for one agent, and the listing computed
viraj-inbound as its own counterpart. Four things are pinned here:

  * the normalizer decodes JSON-text nested objects and maps a bare
    scheduling word; the lints survive any leftover shape;
  * an advisory lint crash can never turn a landed write into FAILED;
  * store.write_agent / write_agent_dir refuse a call_direction that
    contradicts a twin slug's suffix (standalone slugs stay free);
  * list_agents derives a twin's counterpart from the slug, never the data.
"""
from __future__ import annotations

import asyncio
import shutil
import sys

import pytest
import yaml

from conftest import AGENTS_DIR, REPO_ROOT


def _dash():
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)


# ── store: the invariant at the write choke point ───────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    agents_dir = tmp_path / "agents"
    for slug in ("pramukh", "acme-inbound", "acme-outbound"):
        (agents_dir / slug).mkdir(parents=True)
        shutil.copy(AGENTS_DIR / "pramukh" / "agent.yaml",
                    agents_dir / slug / "agent.yaml")
    for slug, direction in (("acme-inbound", "inbound"), ("acme-outbound", "outbound")):
        p = agents_dir / slug / "agent.yaml"
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        d["call_direction"] = direction
        p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return agents_dir


def test_slug_algebra():
    from app.agent_definition import store
    assert store.slug_direction("acme-inbound") == "inbound"
    assert store.slug_direction("acme-outbound") == "outbound"
    assert store.slug_direction("pramukh_agent") is None
    assert store.twin_of_slug("acme-inbound") == "acme-outbound"
    assert store.twin_of_slug("acme-outbound") == "acme-inbound"
    assert store.twin_of_slug("pramukh_agent") is None


def test_write_refuses_a_direction_that_contradicts_the_slug(env):
    from app.agent_definition import store
    data, rev = store.read_agent("acme-inbound", env)
    before_text = (env / "acme-inbound" / "agent.yaml").read_text(encoding="utf-8")
    before_revs = len(store.list_revisions("acme-inbound"))
    data["call_direction"] = "outbound"
    with pytest.raises(store.DirectionLocked) as exc:
        store.write_agent("acme-inbound", data, actor="tester:copilot",
                          base_rev=rev, reason="flip", agents_dir=env)
    msg = str(exc.value)
    assert "acme-inbound" in msg and "inbound version" in msg
    assert "acme-outbound" in msg          # the twin to edit instead is named
    # refused BEFORE the snapshot: no file change, no revision row
    assert (env / "acme-inbound" / "agent.yaml").read_text(encoding="utf-8") == before_text
    assert len(store.list_revisions("acme-inbound")) == before_revs


def test_write_accepts_the_matching_direction_and_fills_a_missing_one(env):
    from app.agent_definition import store
    data, rev = store.read_agent("acme-outbound", env)
    data["identity"]["company_tagline"] = "edited"
    res = store.write_agent("acme-outbound", data, actor="t", base_rev=rev,
                            reason="ok", agents_dir=env)
    assert res["effective"] == "next_call"

    data, rev = store.read_agent("acme-outbound", env)
    data.pop("call_direction", None)   # absent → the schema would say inbound
    store.write_agent("acme-outbound", data, actor="t", base_rev=rev,
                      reason="no field", agents_dir=env)
    after, _ = store.read_agent("acme-outbound", env)
    assert after["call_direction"] == "outbound"


def test_standalone_slug_may_carry_either_direction(env):
    from app.agent_definition import store
    data, rev = store.read_agent("pramukh", env)
    data["call_direction"] = "outbound"
    store.write_agent("pramukh", data, actor="t", base_rev=rev, reason="free", agents_dir=env)
    after, _ = store.read_agent("pramukh", env)
    assert after["call_direction"] == "outbound"


def test_rollback_to_a_contradicting_snapshot_is_refused(env):
    """Rollback goes through write_agent, so a snapshot taken while the twin
    was corrupted cannot re-corrupt it."""
    from app.agent_definition import store
    data, rev = store.read_agent("acme-inbound", env)
    data["identity"]["company_tagline"] = "v2"
    store.write_agent("acme-inbound", data, actor="t", base_rev=rev, reason="v2", agents_dir=env)
    snap = store.list_revisions("acme-inbound")[0]
    # forge the snapshot's text into the corrupted shape
    from shared.db.database import get_db
    full = store.get_revision("acme-inbound", snap["id"])
    bad = yaml.safe_load(full["yaml_text"]); bad["call_direction"] = "outbound"
    with get_db() as db:
        db.execute("UPDATE agent_revisions SET yaml_text=? WHERE id=?",
                   (yaml.safe_dump(bad, allow_unicode=True, sort_keys=False), snap["id"]))
    with pytest.raises(store.DirectionLocked):
        store.rollback_agent("acme-inbound", snap["id"], actor="t", agents_dir=env)


# ── builder: create, PUT and the listing ────────────────────────────────────

@pytest.fixture()
def builder(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    _dash()
    import agent_builder_api as mod
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    monkeypatch.setattr(mod, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(mod, "_ctx_owner", lambda: "tenant@example.com")
    monkeypatch.setattr(mod, "_ctx_is_admin", lambda: False)
    monkeypatch.setattr(mod, "_claim_slug", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_visible_slugs", lambda owner: None)
    monkeypatch.setattr(mod, "_read_active", lambda: None)
    from app.agent_definition.registry import agent_registry
    agent_registry.invalidate_all()
    return mod, agents_dir


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    async def json(self):
        return self._body


def _pramukh() -> dict:
    with open(AGENTS_DIR / "pramukh" / "agent.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _create_acme(mod):
    return asyncio.run(mod.create_agent(_Req({**_pramukh(), "name": "acme",
                                              "call_direction": "outbound"})))


def test_builder_put_refuses_flipping_a_twin(builder):
    from fastapi import HTTPException
    mod, agents_dir = builder
    _create_acme(mod)
    body = yaml.safe_load((agents_dir / "acme-inbound" / "agent.yaml").read_text(encoding="utf-8"))
    body["call_direction"] = "outbound"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.update_agent("acme-inbound", _Req(body)))
    assert exc.value.status_code == 422
    assert "acme-outbound" in str(exc.value.detail)
    assert "Schema validation failed" not in str(exc.value.detail)
    on_disk = yaml.safe_load((agents_dir / "acme-inbound" / "agent.yaml").read_text(encoding="utf-8"))
    assert on_disk["call_direction"] == "inbound"


def test_single_create_under_a_twin_name_must_match_its_suffix(builder):
    from fastapi import HTTPException
    mod, agents_dir = builder
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.create_agent(_Req({**_pramukh(), "name": "solo-outbound",
                                           "twin": False, "call_direction": "inbound"})))
    assert exc.value.status_code == 422
    assert not (agents_dir / "solo-outbound").exists()


def test_listing_derives_the_counterpart_from_the_slug(builder):
    """A twin corrupted the §60 way must still point at its real twin — not
    at itself — so the family can be repaired and the card never offers a
    create that would 409."""
    mod, agents_dir = builder
    _create_acme(mod)
    p = agents_dir / "acme-inbound" / "agent.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8")); d["call_direction"] = "outbound"
    p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    from app.agent_definition.registry import agent_registry
    agent_registry.invalidate_all()

    rows = {a["name"]: a for a in asyncio.run(mod.list_agents())["agents"]}
    assert rows["acme-inbound"]["counterpart"] == "acme-outbound"
    assert rows["acme-inbound"]["counterpart_direction"] == "outbound"
    assert rows["acme-inbound"]["has_counterpart"] is True
    assert rows["acme-outbound"]["counterpart"] == "acme-inbound"
    # a standalone slug still derives from its data
    (agents_dir / "pramukh").mkdir()
    shutil.copy(AGENTS_DIR / "pramukh" / "agent.yaml", agents_dir / "pramukh" / "agent.yaml")
    rows = {a["name"]: a for a in asyncio.run(mod.list_agents())["agents"]}
    assert rows["pramukh"]["counterpart"] in ("pramukh-inbound", "pramukh-outbound")


# ── copilot: the input shape that started it, and the lie it produced ──────

@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    _dash()
    import agent_copilot_api as cop_mod
    return cop_mod


JSON_TEXT = '{ "enabled": false }'


def test_normalizer_decodes_a_nested_object_sent_as_json_text(cop):
    notes: list[str] = []
    d = {"call_flow": {"scheduling": JSON_TEXT, "call_goal": "survey"},
         "guardrails": {"honesty": '{"knowledge_grounding": "strict"}'}}
    cop._normalize_definition(d, notes)
    assert d["call_flow"]["scheduling"] == {"enabled": False}
    assert d["guardrails"]["honesty"]["knowledge_grounding"] == "strict"
    assert any("decoded" in n for n in notes)


def test_normalizer_reads_a_bare_scheduling_word(cop):
    for word, want in (("flexible", {"style": "flexible"}), ("false", {"enabled": False}),
                       ("off", {"enabled": False})):
        d = {"call_flow": {"scheduling": word}}
        cop._normalize_definition(d, [])
        assert d["call_flow"]["scheduling"] == want, word
    notes: list[str] = []
    d = {"call_flow": {"scheduling": "whenever the caller likes"}}
    cop._normalize_definition(d, notes)
    assert "scheduling" not in d.get("call_flow", {})
    assert any("scheduling" in n and "unusable" in n for n in notes)


def test_per_language_spoken_fields_still_accept_a_bare_string(cop):
    d = {"outbound": {"opening_greeting": "नमस्कार {contact_name} जी"}}
    cop._normalize_definition(d, [])
    assert d["outbound"]["opening_greeting"]      # not dropped by the generic pass


def test_quality_lints_survive_a_leftover_text_scheduling(cop):
    merged = cop._default_definition("t", {
        "call_direction": "outbound",
        "languages": {"supported": ["en"], "default": "en"},
        "call_flow": {"scheduling": JSON_TEXT, "stages": [
            {"id": "S1", "order": 0, "goal": "ask the caller about the fair"}]},
    })
    merged["call_flow"]["scheduling"] = JSON_TEXT   # bypass the normalizer on purpose
    merged["calendar"] = "none"
    assert isinstance(cop._quality_warnings(merged), list)


def test_safe_quality_reports_a_crash_instead_of_raising(cop, monkeypatch):
    def boom(*a, **k):
        raise AttributeError("'str' object has no attribute 'get'")
    monkeypatch.setattr(cop, "_quality_warnings", boom)
    out = cop._safe_quality({})
    assert len(out) == 1 and "unavailable" in out[0] and "saved" in out[0]


def test_update_draft_records_the_incident_payload(cop):
    """The exact shape the Viraj session sent, eight times, and was told
    'DID NOT HAPPEN' each time while the draft silently took it."""
    session = {"messages": []}
    guard = {"user_turns": 1, "created_this_turn": False, "session": session,
             "user_message": "भारत रोजगार सेवा — रोजगार स्थिति फॉलो-अप कॉल"}
    res, _s, ok, _m = asyncio.run(cop._execute_tool("update_draft", {
        "source": "confirmed",
        "changes": {
            "call_direction": "outbound",
            "identity": {"agent_name": "Viraj", "company_name": "भारत रोजगार सेवा"},
            "call_flow": {"call_goal": "रोजगार स्थिति अपडेट करना",
                          "scheduling": JSON_TEXT},
            "languages": {"supported": ["hi", "en"], "default": "hi"},
        }}, guard))
    assert ok, res
    assert res["status"] == "draft_updated"
    draft = session["interview"]["draft"]
    assert draft["call_direction"] == "outbound"
    assert draft["call_flow"]["scheduling"] == {"enabled": False}
