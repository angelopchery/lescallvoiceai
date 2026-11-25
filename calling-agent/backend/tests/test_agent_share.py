"""Share an agent by link; the recipient imports an INDEPENDENT copy.

The one property everything here defends: an imported agent has no
relationship to its source. New slug, new agent_id, own history, own owner —
and edits on either side stay put. The rest of the file is the ways that can
quietly stop being true: a shared agent_id silently redirecting another
tenant's document lookups, half an imported twin pair surviving a failure, a
double-clicked Import consuming two plan slots, a revoked link still working.

Two tiers:
  · pure — slug/id/spec algebra and the login-redirect guard. No DB, no I/O.
  · DB   — the full create → preview → import path against lescall_test
           (pg_test_db), with agents/ redirected at a tmp tree.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO), str(_REPO / "calling-agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent_builder_api as builder  # noqa: E402
import agent_share_api as share  # noqa: E402

def _run(coro):
    """The repo's convention for exercising an async handler from a sync test
    (see test_copilot_edit_engine.py) — pytest-asyncio is not installed."""
    import asyncio
    return asyncio.run(coro)



# ── the definition every test copies around ─────────────────────────────────

def _spec(direction: str = "inbound", name: str = "Asha") -> dict:
    return {
        "agent_id": "acme-v1",
        "version": "1.0.0",
        "call_direction": direction,
        "revision": 47,
        "updated_at": "2026-01-01T00:00:00Z",
        "updated_by": "someone@else.com",
        "identity": {"agent_name": name, "company_name": "Acme Homes"},
        "languages": {"supported": ["en"], "default": "en",
                      "tts_voices": {"en": {"model": "bulbul:v3",
                                            "speaker": "shubh"}}},
        "call_flow": {"stages": [{"id": "GREETING", "order": 0, "goal": "hello"},
                                 {"id": "CLOSING", "order": 1, "goal": "bye"}]},
        "response_bank": {"opening_greeting": "Hello"},
        "knowledge_base": {"locations": ["Surat"]},
    }


# ══════════════════════════════════════════════════════ pure ════════════════

def test_sanitize_family_never_yields_an_empty_or_unsafe_slug():
    assert share._sanitize_family("Acme  Homes!! Pvt") == "acme-homes-pvt"
    assert share._sanitize_family("../../etc/passwd") == "etc-passwd"
    # A name that sanitizes away entirely must still produce a usable folder
    # name — "" would write the import into the agents/ root.
    assert share._sanitize_family("---") == "agent"
    assert share._sanitize_family("") == "agent"


def test_prepare_spec_strips_history_and_remints_identity(tmp_path):
    src = tmp_path / "agent.yaml"
    src.write_text(yaml.safe_dump(_spec()), encoding="utf-8")

    out = share._prepare_spec(src, "newco-inbound", set())

    # History belongs to the SOURCE record. A copy claiming revision 47 and
    # "updated_by someone@else.com" makes the importer's first real edit look
    # like a rollback.
    assert out["revision"] == 1
    assert "updated_at" not in out
    assert "updated_by" not in out
    assert out["agent_id"] == "newco-inbound-v1"
    # Everything that describes BEHAVIOUR travels untouched.
    assert out["identity"]["agent_name"] == "Asha"
    assert out["call_flow"]["stages"][0]["goal"] == "hello"


def test_agent_id_is_reminted_away_from_a_collision(tmp_path):
    """documents/store.get_folder_by_agent_id() scans agent.yaml files and
    returns the FIRST agent_id match, so two agents sharing an id means one
    tenant's RAG lookups can resolve into another tenant's document folder."""
    src = tmp_path / "agent.yaml"
    src.write_text(yaml.safe_dump(_spec()), encoding="utf-8")

    taken = {"newco-inbound-v1"}
    out = share._prepare_spec(src, "newco-inbound", taken)
    assert out["agent_id"] != "newco-inbound-v1"
    # …and the caller's set is updated, so a second variant in the same import
    # cannot collide with the first one either.
    assert out["agent_id"] in taken


def test_prepare_spec_rejects_a_definition_that_is_not_a_mapping(tmp_path):
    src = tmp_path / "agent.yaml"
    src.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(Exception):
        share._prepare_spec(src, "newco", set())


@pytest.mark.parametrize("path", [
    "//evil.example",            # protocol-relative: a browser treats it as a host
    "https://evil.example",
    "/\\evil.example",
    "/x\\y",
    "/login",                    # would bounce forever
    "/login?next=/x",
    "/auth/callback",
    "",
    "/" + "a" * 600,             # absurd length
    # Browser-initiated asset fetches: their bounce off the login wall used to
    # overwrite the stashed destination, sending fresh logins to /favicon.ico.
    "/favicon.ico",
    "/logo.png",
    "/assets/index-Ck2fA9.js",
])
def test_login_return_path_refuses_open_redirects(path):
    import auth
    assert auth.safe_return_path(path) == ""


@pytest.mark.parametrize("path", [
    "/s/abc123",
    "/build/agents?import=abc123",
    "/contacts/42",
])
def test_login_return_path_accepts_same_origin_deep_links(path):
    import auth
    assert auth.safe_return_path(path) == path


# ══════════════════════════════════════════════════════ DB ══════════════════

from tests.test_intel_queue import pg_test_db  # noqa: E402,F401  (fixture)

SHARER = "sharer@example.com"
TAKER = "taker@example.com"


@pytest.fixture(autouse=True)
def _isolated(pg_test_db, monkeypatch, tmp_path):
    """Point the DB at lescall_test and agents/ at a scratch tree.

    Without the second half these tests would write real agent folders into
    the repo and hand them ownership rows in whatever database happened to be
    configured.
    """
    monkeypatch.setenv("LESCALL_DB_URL", pg_test_db)
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents)
    monkeypatch.setattr(share, "_AGENTS_DIR", agents)
    monkeypatch.setattr(share, "_ARCHIVE_DIR", agents / "_archived")
    monkeypatch.setattr(share, "_TABLES_READY", True)
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM agent_share_imports")
        db.execute("DELETE FROM agent_shares")
        db.execute("DELETE FROM agents")
        # _grant_headroom() writes here and nothing took it back, so whether a
        # later test's TAKER was on the free plan depended on test ORDER.
        db.execute("DELETE FROM entitlements WHERE owner IN (?,?,?)",
                   (SHARER, TAKER, "third@example.com"))
    yield agents


def _seed_family(agents_dir: Path, family: str, owner: str,
                 directions=("inbound", "outbound")) -> list[str]:
    """Write a family to disk and claim it, the way a real create would."""
    slugs = []
    for d in directions:
        slug = f"{family}-{d}" if d else family
        (agents_dir / slug).mkdir(parents=True)
        (agents_dir / slug / "agent.yaml").write_text(
            yaml.safe_dump(_spec(d or "inbound")), encoding="utf-8")
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("INSERT INTO agents (slug, owner, display_name) "
                       "VALUES (?,?,?)", (slug, owner, "Asha"))
        slugs.append(slug)
    return slugs


def _grant_headroom(owner: str) -> None:
    """Put `owner` on a paid plan for the duration of a test.

    A tenant with no entitlements row resolves to Free, and Free's 1-agent cap
    binds regardless of the global enforcement switch (the trial floor — see
    shared/billing/entitlements.py). Tests about SLUG MINTING must not be
    silently testing the cap instead, so they say out loud that this tenant is
    not on the free plan.
    """
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO entitlements (owner, plan_code, max_agent_families) "
            "VALUES (?, 'enterprise', NULL) ON CONFLICT (owner) DO UPDATE SET "
            "plan_code='enterprise', max_agent_families=NULL", (owner,))


class _Req:
    """Minimal stand-in for the parts of Request these handlers touch."""

    def __init__(self, owner: str, body: dict | None = None):
        self.state = type("S", (), {"owner": owner})()
        self._body = body or {}
        self.base_url = "https://agent.lescall.com/"
        self.client = None

    async def json(self):
        return self._body


def _create_link(family_slug: str, owner: str) -> dict:
    res = _run(share.create_link(_Req(owner, {"slug": family_slug})))
    return res["link"]


# ── the sharer's end ────────────────────────────────────────────────────────

def test_create_link_is_idempotent(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    first = _create_link("acme-inbound", SHARER)
    second = _create_link("acme-outbound", SHARER)
    # Same FAMILY, so the same link — otherwise revoking "the" link would
    # leave a second one live that the user never knew they made.
    assert first["token"] == second["token"]
    assert first["url"].endswith(f"/s/{first['token']}")


def test_cannot_share_an_agent_you_do_not_own(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _create_link("acme-inbound", TAKER)
    # 404, not 403: never confirm that another tenant's agent exists.
    assert exc.value.status_code == 404


# ── the recipient's end ─────────────────────────────────────────────────────

def test_import_creates_an_independent_copy(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)

    result = _run(share.import_share(link["token"], _Req(TAKER)))

    assert result["family"] != "acme"
    assert len(result["slugs"]) == 2          # both directions travel together
    for slug in result["slugs"]:
        assert (_isolated / slug / "agent.yaml").exists()
        assert builder._slug_owner(slug) == TAKER

    # THE property: editing the source does not touch the copy.
    src = _isolated / "acme-inbound" / "agent.yaml"
    edited = yaml.safe_load(src.read_text(encoding="utf-8"))
    edited["response_bank"]["opening_greeting"] = "CHANGED BY THE SHARER"
    src.write_text(yaml.safe_dump(edited), encoding="utf-8")

    copy = yaml.safe_load(
        (_isolated / result["slugs"][0] / "agent.yaml").read_text(encoding="utf-8"))
    assert copy["response_bank"]["opening_greeting"] == "Hello"
    # …and the two records do not share an identity.
    assert copy["agent_id"] != "acme-v1"


def test_imported_agent_is_never_the_accounts_active_fallback(_isolated):
    """agents.is_active defaults to 1 and the bridge picks the most recently
    updated is_active row as the owner's unbound-call agent. Importing an
    agent to look at it must not take over answering their calls."""
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    result = _run(share.import_share(link["token"], _Req(TAKER)))

    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, is_active FROM agents WHERE owner=?", (TAKER,)).fetchall()
    assert rows and all(not r["is_active"] for r in rows)
    assert {r["slug"] for r in rows} == set(result["slugs"])


def test_documents_travel_with_the_agent(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    docs = _isolated / "acme-inbound" / "documents" / "d1"
    docs.mkdir(parents=True)
    (docs / "text.txt").write_text("brochure", encoding="utf-8")
    (docs / "chunks.json").write_text('["brochure"]', encoding="utf-8")
    (docs.parent / "index.json").write_text(
        json.dumps([{"doc_id": "d1", "filename": "b.pdf"}]), encoding="utf-8")

    link = _create_link("acme-inbound", SHARER)
    result = _run(share.import_share(link["token"], _Req(TAKER)))

    inbound = next(s for s in result["slugs"] if s.endswith("-inbound"))
    copied = _isolated / inbound / "documents" / "d1" / "text.txt"
    assert copied.read_text(encoding="utf-8") == "brochure"


def test_routing_and_bookings_do_not_travel(_isolated):
    """routing.yaml is the sharer's DID binding and bookings.json is their
    customers' appointments. Copying either hands the importer's brand-new
    agent someone else's phone number and someone else's calendar."""
    _seed_family(_isolated, "acme", SHARER)
    (_isolated / "acme-inbound" / "routing.yaml").write_text(
        "did: '+918065953160'\n", encoding="utf-8")
    (_isolated / "acme-inbound" / "bookings.json").write_text(
        '[{"caller_phone": "+919999999999"}]', encoding="utf-8")

    link = _create_link("acme-inbound", SHARER)
    result = _run(share.import_share(link["token"], _Req(TAKER)))

    inbound = next(s for s in result["slugs"] if s.endswith("-inbound"))
    assert not (_isolated / inbound / "routing.yaml").exists()
    assert not (_isolated / inbound / "bookings.json").exists()


def test_importing_your_own_link_is_refused(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(SHARER)))
    assert exc.value.status_code == 409


def test_importing_twice_does_not_consume_two_slots(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(TAKER)))
    assert exc.value.status_code == 409

    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute("SELECT slug FROM agents WHERE owner=?", (TAKER,)).fetchall()
    assert {r["slug"] for r in rows} == set(first["slugs"])


def test_reimport_is_allowed_once_the_earlier_copy_is_archived(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))

    # Archive it the way DELETE /api/agents/{name} does: folder moved out,
    # row kept but stamped.
    from shared.db.database import get_db
    for slug in first["slugs"]:
        shutil.rmtree(_isolated / slug)
        with get_db() as db:
            db.execute("UPDATE agents SET archived_at=CURRENT_TIMESTAMP "
                       "WHERE slug=?", (slug,))

    again = _run(share.import_share(link["token"], _Req(TAKER)))
    assert set(again["slugs"]) != set(first["slugs"])   # fresh, unused slugs


def test_revoked_link_is_gone_not_merely_missing(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    _run(share.revoke_link(link["token"], _Req(SHARER)))

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.preview_share(link["token"], _Req(TAKER)))
    # 410, not 404: "this was revoked" and "this never existed" need
    # different next steps from the person holding the link.
    assert exc.value.status_code == 410


def test_expired_link_stops_working(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE agent_shares SET expires_at = CURRENT_TIMESTAMP "
                   "- INTERVAL '1 day' WHERE token=?", (link["token"],))

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(TAKER)))
    assert exc.value.status_code == 410


def test_link_dies_with_the_source_agent(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    for slug in ("acme-inbound", "acme-outbound"):
        shutil.rmtree(_isolated / slug)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.preview_share(link["token"], _Req(TAKER)))
    assert exc.value.status_code == 410


def test_import_walks_past_every_name_already_taken(_isolated):
    """Slugs are globally unique across ALL tenants, so the minted name has to
    dodge the source family, the importer's own agents, and anyone else's —
    silently landing on a taken slug would write into a live agent's folder."""
    _seed_family(_isolated, "acme", SHARER)          # blocks "acme"
    _seed_family(_isolated, "acme-2", TAKER)         # blocks the next candidate
    _seed_family(_isolated, "acme-3", "third@example.com")   # and the one after
    link = _create_link("acme-inbound", SHARER)
    # This test is about name collision, not about plan caps: TAKER already
    # holds a family, which the free tier's floor would refuse outright.
    _grant_headroom(TAKER)

    result = _run(share.import_share(link["token"], _Req(TAKER)))

    assert result["family"] == "acme-4"
    for slug in result["slugs"]:
        assert builder._slug_owner(slug) == TAKER


def test_preview_reports_the_plan_cap_instead_of_failing(_isolated,
                                                               monkeypatch):
    """At cap the popup must still render the agent AND say why Import is
    unavailable — an HTTP error would give the recipient a dead dialog."""
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)

    from shared.billing import entitlements
    from shared.billing.errors import CODE_CAP_AGENTS, Decision
    monkeypatch.setattr(entitlements, "check_agent_create",
                        lambda owner, family="": Decision.deny(
                            CODE_CAP_AGENTS, "Your plan includes 1 agent."))

    preview = _run(share.preview_share(link["token"], _Req(TAKER)))
    assert preview["can_import"] is False
    assert preview["code"] == CODE_CAP_AGENTS
    assert preview["agent"]["agent_name"] == "Asha"   # still fully rendered


def test_import_is_refused_at_the_plan_cap(_isolated, monkeypatch):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)

    from shared.billing.errors import BillingRefusal, CODE_CAP_AGENTS
    monkeypatch.setattr(builder, "_assert_agent_quota",
                        lambda owner, family="": (_ for _ in ()).throw(
                            BillingRefusal("Your plan includes 1 agent.",
                                           CODE_CAP_AGENTS)))
    with pytest.raises(BillingRefusal) as exc:
        _run(share.import_share(link["token"], _Req(TAKER)))
    assert exc.value.status_code == 402

    # Nothing was written, and the reservation row was released — otherwise
    # upgrading and retrying would hit "already imported" forever.
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute("SELECT 1 FROM agents WHERE owner=?", (TAKER,)).fetchall()
        res = db.execute("SELECT 1 FROM agent_share_imports WHERE importer=?",
                         (TAKER,)).fetchall()
    assert not rows and not res


def test_a_failed_import_leaves_nothing_behind(_isolated, monkeypatch):
    """Half an imported pair is worse than none: a number could bind to the
    survivor and run the wrong direction's prompts."""
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)

    real = builder.write_agent_dir
    calls = {"n": 0}

    def flaky(name, spec, owner):
        calls["n"] += 1
        if calls["n"] == 2:          # second variant blows up mid-import
            raise RuntimeError("disk full")
        return real(name, spec, owner)

    monkeypatch.setattr(builder, "write_agent_dir", flaky)
    with pytest.raises(RuntimeError):
        _run(share.import_share(link["token"], _Req(TAKER)))

    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute("SELECT slug FROM agents WHERE owner=?", (TAKER,)).fetchall()
        res = db.execute("SELECT 1 FROM agent_share_imports WHERE importer=?",
                         (TAKER,)).fetchall()
    assert not rows, f"orphaned ownership rows: {[r['slug'] for r in rows]}"
    assert not res, "import reservation was not released"
    leftovers = [p.name for p in _isolated.iterdir() if p.name.startswith("acme-2")]
    assert not leftovers, f"orphaned folders: {leftovers}"


def test_import_count_tracks_reality(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    _run(share.import_share(link["token"], _Req(TAKER)))
    _run(share.import_share(link["token"], _Req("third@example.com")))

    res = _run(share.get_link(_Req(SHARER), slug="acme-inbound"))
    assert res["link"]["import_count"] == 2


def test_share_refuses_a_slug_that_is_not_a_slug(_isolated):
    """A traversal can never match an owned row, but it reaches
    `_AGENTS_DIR / slug` first — so the shape is rejected at the door."""
    from fastapi import HTTPException
    for bad in ("../../etc/passwd", "a/b", "acme;drop", "x" * 90):
        with pytest.raises(HTTPException) as exc:
            _create_link(bad, SHARER)
        assert exc.value.status_code == 400, bad


# ══════════════════════════════════ opening a link you already imported ══════
#
# The case this section exists for is real: someone built an agent FOR another
# account, kept improving it, and re-sent the link. The recipient was told
# "You already imported this agent" and pointed at the OLD one; they archived
# one direction trying to get rid of it and stayed locked out, because the
# other direction was still live. A second open is now a choice — replace the
# copy, or take a new one — and both halves of that choice have to keep the
# promises the first import makes.

def _read(agents_dir: Path, slug: str) -> dict:
    return yaml.safe_load(
        (agents_dir / slug / "agent.yaml").read_text(encoding="utf-8"))


def _sharer_edits(agents_dir: Path, slug: str, greeting: str) -> None:
    spec = _read(agents_dir, slug)
    spec["response_bank"]["opening_greeting"] = greeting
    (agents_dir / slug / "agent.yaml").write_text(
        yaml.safe_dump(spec), encoding="utf-8")


def _archive(agents_dir: Path, slug: str) -> None:
    """Exactly what DELETE /api/agents/{name} does: the folder MOVES to
    _archived/ and the row is stamped, not deleted."""
    (agents_dir / "_archived").mkdir(exist_ok=True)
    shutil.move(str(agents_dir / slug), str(agents_dir / "_archived" / slug))
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE agents SET archived_at=CURRENT_TIMESTAMP "
                   "WHERE slug=?", (slug,))


def _import_rows(importer: str = TAKER) -> list[dict]:
    from shared.db.database import get_db
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM agent_share_imports WHERE importer=? "
            "ORDER BY copy_no", (importer,)).fetchall()]


def test_a_second_open_is_a_choice_not_a_dead_end(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))
    _grant_headroom(TAKER)

    preview = _run(share.preview_share(link["token"], _Req(TAKER)))
    # Holding a copy no longer makes the link unusable…
    assert preview["can_import"] is True
    # …it adds the copy to what the dialog can offer to replace.
    assert [c["family"] for c in preview["copies"]] == [first["family"]]
    assert preview["copies"][0]["primary"] in first["slugs"]
    assert preview["source_updated_at"]

    # A bare POST still refuses: guessing "replace" would overwrite an agent
    # nobody pointed at, and guessing "new" is the double-click bug.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(TAKER)))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "already_imported"
    assert exc.value.detail["copies"][0]["family"] == first["family"]


def test_import_as_new_makes_a_second_independent_copy(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))
    _grant_headroom(TAKER)

    second = _run(share.import_share(link["token"], _Req(TAKER, {"mode": "new"})))

    assert second["family"] != first["family"]
    assert not set(second["slugs"]) & set(first["slugs"])
    for slug in first["slugs"] + second["slugs"]:
        assert (_isolated / slug / "agent.yaml").exists()
        assert builder._slug_owner(slug) == TAKER
    ids = {_read(_isolated, s)["agent_id"] for s in first["slugs"] + second["slugs"]}
    assert len(ids) == 4, "every copy needs its own agent_id (RAG folder lookup)"

    rows = _import_rows()
    assert [r["copy_no"] for r in rows] == [1, 2]
    assert [r["new_family"] for r in rows] == [first["family"], second["family"]]
    # Both are offered for replacement afterwards — a re-pointed single row
    # would have silently forgotten the first.
    preview = _run(share.preview_share(link["token"], _Req(TAKER)))
    assert [c["family"] for c in preview["copies"]] == [first["family"],
                                                        second["family"]]


def test_replace_overwrites_the_copy_and_keeps_what_is_mine(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))
    inbound = next(s for s in first["slugs"] if s.endswith("-inbound"))
    mine_before = _read(_isolated, inbound)

    # The recipient makes it theirs: an edit, a number binding, a booking, and
    # they switch it on.
    _sharer_edits(_isolated, inbound, "EDITED BY THE RECIPIENT")
    (_isolated / inbound / "routing.yaml").write_text("did: '+911'\n", encoding="utf-8")
    (_isolated / inbound / "bookings.json").write_text('[{"x": 1}]', encoding="utf-8")
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE agents SET is_active=1 WHERE slug=?", (inbound,))
    # Meanwhile the sharer ships a new version.
    _sharer_edits(_isolated, "acme-inbound", "VERSION TWO")
    _sharer_edits(_isolated, "acme-outbound", "VERSION TWO OUT")

    result = _run(share.import_share(
        link["token"], _Req(TAKER, {"mode": "replace"})))

    assert result["status"] == "replaced"
    assert result["family"] == first["family"]            # same agent, same name
    assert set(result["replaced"]) == set(first["slugs"])
    after = _read(_isolated, inbound)
    assert after["response_bank"]["opening_greeting"] == "VERSION TWO"
    outbound = next(s for s in first["slugs"] if s.endswith("-outbound"))
    assert _read(_isolated, outbound)["response_bank"]["opening_greeting"] \
        == "VERSION TWO OUT"
    assert _read(_isolated, outbound)["call_direction"] == "outbound"

    # Identity is the COPY's. The source's agent_id here would send this
    # tenant's document lookups into the sharer's folder.
    assert after["agent_id"] == mine_before["agent_id"] != "acme-v1"
    assert after["updated_by"] == f"{TAKER}:share"
    # Everything that is the recipient's survives.
    assert (_isolated / inbound / "routing.yaml").read_text(encoding="utf-8") == "did: '+911'\n"
    assert (_isolated / inbound / "bookings.json").exists()
    with get_db() as db:
        row = db.execute("SELECT is_active, owner FROM agents WHERE slug=?",
                         (inbound,)).fetchone()
        snap = db.execute(
            "SELECT yaml_text, reason FROM agent_revisions WHERE slug=? "
            "ORDER BY id DESC LIMIT 1", (inbound,)).fetchone()
    assert row["is_active"] and row["owner"] == TAKER
    # …including the version they just lost: one rollback away.
    assert "EDITED BY THE RECIPIENT" in snap["yaml_text"]
    assert SHARER in snap["reason"]

    # No new agent, no new row, no inflated counter.
    assert len(_import_rows()) == 1
    assert _import_rows()[0]["sync_count"] == 1
    assert _import_rows()[0]["synced_at"]
    with get_db() as db:
        n = db.execute("SELECT COUNT(*) AS n FROM agents WHERE owner=?",
                       (TAKER,)).fetchone()["n"]
    assert n == 2
    assert _run(share.get_link(_Req(SHARER), slug="acme-inbound")
                )["link"]["import_count"] == 1


def test_replace_brings_back_a_direction_the_recipient_archived(_isolated):
    """The production case. They archived ONE half trying to delete the old
    agent; the other half kept the link locked. Replace has to hand back the
    whole shared agent — the alternative is a twin ADAPTED from the inbound
    script when the sharer wrote a real outbound one."""
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))
    inbound = next(s for s in first["slugs"] if s.endswith("-inbound"))
    outbound = next(s for s in first["slugs"] if s.endswith("-outbound"))
    _archive(_isolated, outbound)
    _sharer_edits(_isolated, "acme-outbound", "NEW OUTBOUND SCRIPT")

    # Still locked without a mode — one live half is a live copy.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(TAKER)))
    assert exc.value.status_code == 409

    # TAKER is on the FREE plan (no entitlements row): replace must not need a
    # slot, including for the half it adds back to a family they already hold.
    result = _run(share.import_share(
        link["token"], _Req(TAKER, {"mode": "replace", "target": first["family"]})))

    assert result["replaced"] == [inbound]
    assert result["added"] == [outbound]
    back = _read(_isolated, outbound)
    assert back["response_bank"]["opening_greeting"] == "NEW OUTBOUND SCRIPT"
    assert back["call_direction"] == "outbound"
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT archived_at, is_active, owner FROM agents "
                         "WHERE slug=?", (outbound,)).fetchone()
    assert row["archived_at"] is None, "on disk but still 'archived' in the index"
    assert not row["is_active"]
    assert row["owner"] == TAKER
    # Their archived version is not destroyed by getting a new one.
    assert (_isolated / "_archived" / outbound / "agent.yaml").exists()
    preview = _run(share.preview_share(link["token"], _Req(TAKER)))
    assert set(preview["copies"][0]["slugs"]) == {inbound, outbound}


def test_at_the_plan_cap_replace_works_and_new_does_not(_isolated):
    """Every free account with one agent is AT its cap. If replace needed a
    slot, the people most likely to be sent an agent could never update it."""
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    _run(share.import_share(link["token"], _Req(TAKER)))      # uses the 1 slot
    _sharer_edits(_isolated, "acme-inbound", "VERSION TWO")

    preview = _run(share.preview_share(link["token"], _Req(TAKER)))
    assert preview["can_import"] is False and preview["code"] == "cap_agents"
    assert len(preview["copies"]) == 1                        # …but replaceable

    from shared.billing.errors import BillingRefusal
    with pytest.raises(BillingRefusal):
        _run(share.import_share(link["token"], _Req(TAKER, {"mode": "new"})))
    assert len(_import_rows()) == 1, "refused import left a reservation behind"

    result = _run(share.import_share(
        link["token"], _Req(TAKER, {"mode": "replace"})))
    assert result["status"] == "replaced"


def test_replace_must_be_told_which_copy_when_there_are_two(_isolated):
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))
    _grant_headroom(TAKER)
    second = _run(share.import_share(link["token"], _Req(TAKER, {"mode": "new"})))
    _sharer_edits(_isolated, "acme-inbound", "VERSION TWO")

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(TAKER, {"mode": "replace"})))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "target_required"

    _run(share.import_share(
        link["token"], _Req(TAKER, {"mode": "replace", "target": second["family"]})))
    greeting = lambda fam: _read(_isolated, f"{fam}-inbound")[   # noqa: E731
        "response_bank"]["opening_greeting"]
    assert greeting(second["family"]) == "VERSION TWO"
    assert greeting(first["family"]) == "Hello"               # untouched


def test_replace_can_only_ever_target_my_own_copy_of_this_link(_isolated):
    """`target` is matched against the caller's live copies OF THIS SHARE and
    nothing else — not the source, not another tenant's agent, not one of the
    caller's own unrelated agents."""
    _seed_family(_isolated, "acme", SHARER)
    _seed_family(_isolated, "victim", "third@example.com")
    _seed_family(_isolated, "unrelated", TAKER)
    _grant_headroom(TAKER)
    link = _create_link("acme-inbound", SHARER)
    _run(share.import_share(link["token"], _Req(TAKER)))
    _sharer_edits(_isolated, "acme-inbound", "VERSION TWO")

    from fastapi import HTTPException
    for target in ("victim", "acme", "unrelated", "../victim"):
        with pytest.raises(HTTPException) as exc:
            _run(share.import_share(
                link["token"], _Req(TAKER, {"mode": "replace", "target": target})))
        assert exc.value.status_code == 400, target
    for untouched in ("victim-inbound", "unrelated-inbound"):
        assert _read(_isolated, untouched)["response_bank"][
            "opening_greeting"] == "Hello"

    # And someone who never imported it has nothing to replace.
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(
            link["token"], _Req("third@example.com", {"mode": "replace"})))
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(TAKER, {"mode": "merge"})))
    assert exc.value.status_code == 400


def test_a_failed_replace_puts_everything_back(_isolated, monkeypatch):
    """A pair running two different versions of one script is the state this
    feature gets people OUT of — a half-applied replace must not create it."""
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))
    _sharer_edits(_isolated, "acme-inbound", "VERSION TWO")
    _sharer_edits(_isolated, "acme-outbound", "VERSION TWO OUT")

    from backend.app.agent_definition import store as agent_store
    real = agent_store.write_agent
    calls = {"n": 0}

    def flaky(slug, new_dict, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real(slug, new_dict, **kw)

    monkeypatch.setattr(agent_store, "write_agent", flaky)
    with pytest.raises(RuntimeError):
        _run(share.import_share(link["token"], _Req(TAKER, {"mode": "replace"})))

    for slug in first["slugs"]:
        assert _read(_isolated, slug)["response_bank"]["opening_greeting"] == "Hello"
    assert _import_rows()[0]["sync_count"] == 0


def _add_doc(agents_dir: Path, slug: str, doc_id: str, text: str,
             uploaded_at: str) -> None:
    folder = agents_dir / slug / "documents"
    (folder / doc_id).mkdir(parents=True, exist_ok=True)
    (folder / doc_id / "text.txt").write_text(text, encoding="utf-8")
    index = folder / "index.json"
    entries = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    entries = [e for e in entries if e["doc_id"] != doc_id]
    entries.append({"doc_id": doc_id, "filename": f"{doc_id}.pdf",
                    "uploaded_at": uploaded_at})
    index.write_text(json.dumps(entries), encoding="utf-8")


def _remove_doc(agents_dir: Path, slug: str, doc_id: str) -> None:
    folder = agents_dir / slug / "documents"
    shutil.rmtree(folder / doc_id)
    entries = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    (folder / "index.json").write_text(
        json.dumps([e for e in entries if e["doc_id"] != doc_id]), encoding="utf-8")


def _doc_ids(agents_dir: Path, slug: str) -> set[str]:
    index = agents_dir / slug / "documents" / "index.json"
    return {e["doc_id"] for e in json.loads(index.read_text(encoding="utf-8"))}


@pytest.mark.parametrize("legacy_copy", [False, True])
def test_replace_syncs_the_knowledge_base_without_eating_my_uploads(
        _isolated, legacy_copy):
    """Three kinds of document, three answers. `legacy_copy` is a copy imported
    before the provenance manifest existed (every copy in production today):
    its first replace has to reach the same answer from upload times alone."""
    _seed_family(_isolated, "acme", SHARER)
    _add_doc(_isolated, "acme-inbound", "old", "2025 price list", "2026-01-01 10:00 UTC")
    _add_doc(_isolated, "acme-inbound", "stay", "brochure v1", "2026-01-01 10:00 UTC")
    link = _create_link("acme-inbound", SHARER)
    first = _run(share.import_share(link["token"], _Req(TAKER)))
    inbound = next(s for s in first["slugs"] if s.endswith("-inbound"))
    if legacy_copy:
        (_isolated / inbound / "documents" / share._SHARED_MANIFEST).unlink()

    _add_doc(_isolated, inbound, "mine", "my own notes", "2099-01-01 10:00 UTC")
    _remove_doc(_isolated, "acme-inbound", "old")             # sharer retires it
    _add_doc(_isolated, "acme-inbound", "stay", "brochure v2", "2026-01-01 10:00 UTC")
    _add_doc(_isolated, "acme-inbound", "new", "2026 price list", "2099-01-01 09:00 UTC")

    result = _run(share.import_share(link["token"], _Req(TAKER, {"mode": "replace"})))

    assert _doc_ids(_isolated, inbound) == {"stay", "new", "mine"}
    docs = _isolated / inbound / "documents"
    assert not (docs / "old").exists(), "a retired price list would still be quoted"
    assert (docs / "stay" / "text.txt").read_text(encoding="utf-8") == "brochure v2"
    assert (docs / "mine" / "text.txt").read_text(encoding="utf-8") == "my own notes"
    assert result["documents"] == {"updated": 2, "removed": 1, "kept": 1}

    # SECOND replace: "new" carries a post-import stamp, so upload time alone
    # would now mistake it for the recipient's. The manifest must not.
    _remove_doc(_isolated, "acme-inbound", "new")
    _run(share.import_share(link["token"], _Req(TAKER, {"mode": "replace"})))
    assert _doc_ids(_isolated, inbound) == {"stay", "mine"}


def test_a_second_click_while_an_import_is_running_is_refused(_isolated):
    """The unique index only converges two requests that reach the INSERT
    together. A click that lands after the first reservation has committed —
    i.e. an ordinary double-click — has to be stopped by the reservation."""
    _seed_family(_isolated, "acme", SHARER)
    link = _create_link("acme-inbound", SHARER)
    share_id = share._fetch_share(link["token"])["id"]
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO agent_share_imports (share_id, importer, source_owner, "
            "source_family, new_family, new_slugs, copy_no) "
            "VALUES (?,?,?,?,?,?,?)",
            (share_id, TAKER, SHARER, "acme", "acme-2", "[]", 1))

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(share.import_share(link["token"], _Req(TAKER)))
    assert exc.value.status_code == 409
    assert not [p for p in _isolated.iterdir() if p.name.startswith("acme-2")]

    # A reservation whose process died must not lock the link forever.
    with get_db() as db:
        db.execute("UPDATE agent_share_imports SET "
                   "created_at = now() - interval '1 hour' WHERE importer=?",
                   (TAKER,))
    result = _run(share.import_share(link["token"], _Req(TAKER)))
    assert result["status"] == "imported"
    assert [r["copy_no"] for r in _import_rows()] == [1, 2]   # never reused


def test_a_bare_slug_travelling_with_a_twin_keeps_its_bare_name(_isolated):
    """A standalone agent that later gained a twin is a mixed family. The
    direction used to come from the suffix alone, which named the bare one
    `<family>-None`."""
    _seed_family(_isolated, "acme", SHARER, directions=("outbound", ""))
    link = _create_link("acme", SHARER)
    result = _run(share.import_share(link["token"], _Req(TAKER)))
    assert set(result["slugs"]) == {f"{result['family']}-outbound",
                                    result["family"]}


def test_recreating_a_slug_you_archived_makes_it_live_again(_isolated):
    """builder._claim_slug: the INSERT is a no-op against the surviving
    archived row, so the re-created agent stayed 'archived' — live on disk,
    uncounted by the plan cap, and 404 to sharing."""
    _seed_family(_isolated, "acme", SHARER, directions=("inbound",))
    _archive(_isolated, "acme-inbound")
    builder._REQ_OWNER.set(SHARER)

    builder.write_agent_dir("acme-inbound", _spec("inbound"), SHARER)

    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT archived_at FROM agents WHERE slug=?",
                         ("acme-inbound",)).fetchone()
    assert row["archived_at"] is None
    assert share._owned_family_slugs("acme", SHARER) == ["acme-inbound"]

    # Owner-scoped: it never resurrects somebody else's archive.
    _seed_family(_isolated, "other", "third@example.com", directions=("inbound",))
    _archive(_isolated, "other-inbound")
    _grant_headroom(TAKER)
    builder._REQ_OWNER.set(TAKER)
    builder.write_agent_dir("other-inbound", _spec("inbound"), TAKER)
    with get_db() as db:
        row = db.execute("SELECT archived_at, owner FROM agents WHERE slug=?",
                         ("other-inbound",)).fetchone()
    assert row["owner"] == "third@example.com" and row["archived_at"] is not None
