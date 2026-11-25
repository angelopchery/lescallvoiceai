"""The Excel export of a call selection.

An export is the artefact people act on after the filter has done its work —
it leaves the product and lands in a dialling team's hands — so the things
tested here are the ones that make it usable or useless: the sheets exist and
carry the right grain (one row per NUMBER, not per call), a phone number stays
a string rather than becoming a float, another tenant's call ids export
nothing, and a transcript longer than Excel's per-cell ceiling is truncated
instead of producing a file Excel refuses to open.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401

OWNER = "export@test.local"
OTHER = "someone-else@test.local"


@pytest.fixture()
def corpus(pg_test_db):
    """One repeat caller (2 calls, 1 contact), one stranger, and one call that
    belongs to a different account."""
    from shared.db.database import get_db

    with get_db() as db:
        for own in (OWNER, OTHER):
            db.execute("DELETE FROM calls WHERE owner=?", (own,))
            db.execute("DELETE FROM contacts WHERE owner=?", (own,))
        contact = db.execute(
            """INSERT INTO contacts (owner, phone, sig_digits, name, call_count,
                                     disposition, notes)
               VALUES (?,?,?,?,?,?,?) RETURNING id""",
            (OWNER, "919876500001", "9876500001", "Repeat Rita", 2,
             "interested", "asked for a Sunday visit"),
        ).fetchone()
        base = dt.datetime(2026, 8, 20, 10, 0, 0)
        ids = {}

        def _call(owner, key, phone, contact_id, minutes, transcript, lead=None):
            row = db.execute(
                """INSERT INTO calls (owner, call_key, conn_id, phone, sig_digits,
                                      direction, agent_slug, started_at,
                                      duration_s, contact_id, transcript, language)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (owner, f"xk-{key}", f"xc-{key}", phone, phone[-10:], "outbound",
                 "hiring-outbound", base + dt.timedelta(minutes=minutes), 120.5,
                 contact_id, transcript, "en-IN"),
            ).fetchone()
            if lead is not None:
                db.execute(
                    """INSERT INTO call_intelligence
                           (call_id, owner, lead_score, sentiment, summary,
                            customer_intent, processed_at)
                       VALUES (?,?,?,?,?,?,now())""",
                    (row["id"], owner, lead, "positive", f"summary for {key}",
                     "relocation"),
                )
            return row["id"]

        ids["rita1"] = _call(OWNER, "rita1", "919876500001", contact["id"], 0,
                             "Caller: yes, I can move to Vapi.", lead=80)
        ids["rita2"] = _call(OWNER, "rita2", "919876500001", contact["id"], 30,
                             "Caller: confirming the visit.", lead=40)
        ids["stranger"] = _call(OWNER, "stranger", "919876500002", None, 60,
                                "Caller: not interested.", lead=5)
        ids["foreign"] = _call(OTHER, "foreign", "919876500003", None, 90,
                               "Caller: this belongs to another tenant.", lead=90)
        ids["contact_id"] = contact["id"]
    return ids


def _build(ids, call_ids, meta=None, evidence=None):
    from shared.db.database import get_db
    from shared.intelligence import selection_export

    with get_db() as db:
        return selection_export.build_for_selection(
            db, OWNER, call_ids,
            {"title": "Test export", "query": "whoever relocated to vapi",
             "created_by": OWNER, **(meta or {})},
            evidence=evidence)


def _sheets(blob: bytes):
    from openpyxl import load_workbook

    return load_workbook(io.BytesIO(blob))


def test_the_workbook_has_one_sheet_per_question(corpus):
    blob, stats = _build(corpus, [corpus["rita1"], corpus["rita2"],
                                  corpus["stranger"]])
    wb = _sheets(blob)
    assert wb.sheetnames == ["Summary", "Numbers", "Contacts", "Calls",
                            "Insights", "Transcripts", "Commitments", "Legend"]
    assert stats["call_count"] == 3


def test_numbers_is_one_row_per_phone_not_per_call(corpus):
    """The reason this sheet exists: a repeat caller is ONE line on a dialling
    list. Exporting three calls from two numbers must produce two rows."""
    blob, stats = _build(corpus, [corpus["rita1"], corpus["rita2"],
                                  corpus["stranger"]])
    ws = _sheets(blob)["Numbers"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 2
    assert stats["number_count"] == 2
    by_phone = {r[0]: r for r in rows}
    rita = by_phone["919876500001"]
    assert rita[1] == "Repeat Rita"
    assert rita[2] == 2, "the repeat caller's two calls collapsed to one row"
    assert rita[3] == pytest.approx(241.0), "talk time is summed across her calls"
    # Hottest first — the sheet is a work queue.
    assert rows[0][0] == "919876500001"


def test_a_phone_number_stays_text(corpus):
    """Excel turns a long numeric string into 9.19877E+11 and silently eats a
    leading +. A dialling list that has done that is worthless."""
    blob, _ = _build(corpus, [corpus["rita1"]])
    ws = _sheets(blob)["Numbers"]
    cell = ws.cell(row=2, column=1)
    assert isinstance(cell.value, str)
    assert cell.number_format == "@"


def test_another_tenants_call_id_exports_nothing(corpus):
    """Ownership is enforced in SQL, so a hand-edited request cannot widen the
    selection."""
    blob, stats = _build(corpus, [corpus["rita1"], corpus["foreign"]])
    assert stats["call_count"] == 1
    assert stats["call_ids"] == [corpus["rita1"]]
    transcripts = list(_sheets(blob)["Transcripts"].iter_rows(min_row=2, values_only=True))
    assert not any("another tenant" in str(r[-1]) for r in transcripts)


def test_the_contacts_sheet_carries_the_phone_book_record(corpus):
    blob, stats = _build(corpus, [corpus["rita1"], corpus["rita2"],
                                  corpus["stranger"]])
    ws = _sheets(blob)["Contacts"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    # Only the known contact; the stranger has no phone-book record to show.
    assert len(rows) == 1 and stats["contact_count"] == 1
    assert rows[0][0] == "Repeat Rita"
    assert "Sunday visit" in str(rows[0][9])
    assert rows[0][-1] == 2, "calls-in-this-export is counted per contact"


def test_every_call_links_back_to_its_workspace(corpus):
    """A spreadsheet row is a dead end unless it can get you back to the call."""
    blob, _ = _build(corpus, [corpus["rita1"]])
    ws = _sheets(blob)["Calls"]
    header = [c.value for c in ws[1]]
    url = ws.cell(row=2, column=header.index("Open in LesCall") + 1).value
    assert url.endswith("/intelligence/xk-rita1")


def test_a_transcript_past_excels_cell_limit_is_truncated(corpus):
    from shared.db.database import get_db
    from shared.intelligence import selection_export

    with get_db() as db:
        db.execute("UPDATE calls SET transcript=? WHERE id=?",
                   ("x" * 40000, corpus["rita1"]))
    blob, _ = _build(corpus, [corpus["rita1"]])
    ws = _sheets(blob)["Transcripts"]
    value = ws.cell(row=2, column=8).value
    assert len(value) <= selection_export.CELL_MAX + 40
    assert value.endswith("[truncated for Excel]")


def test_filter_evidence_becomes_its_own_sheet(corpus):
    """When the selection came from a verified AI filter, the workbook has to
    carry WHY each row is in it — otherwise the list has to be trusted rather
    than checked."""
    evidence = {corpus["rita1"]: {"confidence": 0.93,
                                  "evidence": "yes, I can move to Vapi.",
                                  "reason": "caller agreed"}}
    blob, _ = _build(corpus, [corpus["rita1"], corpus["stranger"]],
                     evidence=evidence)
    wb = _sheets(blob)
    assert "Evidence" in wb.sheetnames
    rows = list(wb["Evidence"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 1, "only the calls the filter actually matched"
    assert "move to Vapi" in rows[0][6]
    assert rows[0][5] == pytest.approx(0.93)


def test_the_summary_sheet_records_the_question_that_produced_the_file(corpus):
    """An export that cannot say what question it answers is an orphan two
    weeks later."""
    blob, _ = _build(corpus, [corpus["rita1"]],
                     meta={"verified": True, "verify_model": "gemini-x-lite",
                           "criterion": {"restated": "Callers who agreed to move to Vapi",
                                         "requirements": ["destination is Vapi"]}})
    ws = _sheets(blob)["Summary"]
    text = "\n".join(f"{r[0]}: {r[1]}" for r in ws.iter_rows(min_row=2, values_only=True))
    assert "whoever relocated to vapi" in text
    assert "Callers who agreed to move to Vapi" in text
    assert "destination is Vapi" in text
    assert "gemini-x-lite" in text


def test_an_empty_selection_builds_an_empty_but_valid_workbook(corpus):
    blob, stats = _build(corpus, [])
    assert stats["call_count"] == 0
    assert stats == {"call_count": 0, "contact_count": 0, "number_count": 0,
                     "call_ids": []}


def test_a_local_copy_is_only_deleted_once_it_is_safely_in_the_bucket(pg_test_db,
                                                                     tmp_path,
                                                                     monkeypatch):
    """The local file is a serving FALLBACK. Deleting one whose upload failed
    would destroy the only copy of an export the user was told they had."""
    import datetime as dt

    from shared import export_store
    from shared.db.database import get_db

    monkeypatch.setattr(export_store, "EXPORTS_DIR", tmp_path)
    # The object is still in the bucket — that check has its own test below.
    monkeypatch.setattr(export_store, "object_exists",
                        lambda obj, bucket=None: True)
    uploaded = tmp_path / "uploaded.xlsx"
    orphan = tmp_path / "orphan.xlsx"
    uploaded.write_bytes(b"x")
    orphan.write_bytes(b"x")
    old = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    with get_db() as db:
        db.execute("DELETE FROM call_exports WHERE owner=?", (OWNER,))
        db.execute(
            """INSERT INTO call_exports (owner, filename, download_token, storage,
                                         gcs_bucket, gcs_object, gcs_uploaded_at,
                                         local_path)
               VALUES (?,?,?,'gcs','b','o',?,?)""",
            (OWNER, "uploaded.xlsx", "tok-uploaded", old, str(uploaded)))
        db.execute(
            """INSERT INTO call_exports (owner, filename, download_token, storage,
                                         local_path, upload_error)
               VALUES (?,?,?,'local',?,'bucket unreachable')""",
            (OWNER, "orphan.xlsx", "tok-orphan", str(orphan)))

    removed = export_store.cleanup_local(max_age_days=0)
    assert removed == 1
    assert not uploaded.exists()
    assert orphan.exists(), "deleted the only copy of an export that never uploaded"


def test_a_local_copy_survives_when_the_object_vanished_from_the_bucket(pg_test_db,
                                                                       tmp_path,
                                                                       monkeypatch):
    """`storage='gcs'` records that the upload succeeded ONCE. This VM's
    service account has object-level access only and cannot read the bucket's
    lifecycle rules, so an operator's delete-after-N-days rule is invisible to
    us. Trusting the flag would let that rule silently destroy the last copy."""
    from shared import export_store
    from shared.db.database import get_db

    monkeypatch.setattr(export_store, "EXPORTS_DIR", tmp_path)
    gone = tmp_path / "gone-from-bucket.xlsx"
    gone.write_bytes(b"x")
    monkeypatch.setattr(export_store, "object_exists",
                        lambda obj, bucket=None: False)
    with get_db() as db:
        db.execute("DELETE FROM call_exports WHERE owner=?", (OWNER,))
        db.execute(
            """INSERT INTO call_exports (owner, filename, download_token, storage,
                                         gcs_bucket, gcs_object, gcs_uploaded_at,
                                         local_path)
               VALUES (?,?,?,'gcs','b','o', now() - interval '99 days', ?)""",
            (OWNER, "gone-from-bucket.xlsx", "tok-gone", str(gone)))

    assert export_store.cleanup_local(max_age_days=0) == 0
    assert gone.exists(), "deleted the last copy of an export GCS no longer has"


def test_an_unreachable_bucket_keeps_the_local_copy(pg_test_db, tmp_path,
                                                    monkeypatch):
    """Cannot confirm is not the same as confirmed gone."""
    from shared import export_store
    from shared.db.database import get_db

    monkeypatch.setattr(export_store, "EXPORTS_DIR", tmp_path)
    f = tmp_path / "unreachable.xlsx"
    f.write_bytes(b"x")

    def _boom(obj, bucket=None):
        raise RuntimeError("GCS unreachable")

    monkeypatch.setattr(export_store, "object_exists", _boom)
    with get_db() as db:
        db.execute("DELETE FROM call_exports WHERE owner=?", (OWNER,))
        db.execute(
            """INSERT INTO call_exports (owner, filename, download_token, storage,
                                         gcs_bucket, gcs_object, gcs_uploaded_at,
                                         local_path)
               VALUES (?,?,?,'gcs','b','o', now() - interval '99 days', ?)""",
            (OWNER, "unreachable.xlsx", "tok-unreachable", str(f)))
    assert export_store.cleanup_local(max_age_days=0) == 0
    assert f.exists()


def test_an_export_is_read_back_from_the_bucket_it_was_written_to(monkeypatch):
    """The runbook offers GCS_EXPORTS_BUCKET as a one-line switch. Resolving
    the bucket from the live config instead of the row would 404 every export
    written before that switch, months later, once its local copy aged out."""
    from shared import export_store

    seen = {}

    class _Blob:
        def __init__(self, name):
            seen["object"] = name

        def open(self, _mode):
            return "stream"

    class _Bucket:
        def __init__(self, name):
            seen["bucket"] = name

        def blob(self, name):
            return _Blob(name)

    monkeypatch.setattr(export_store, "_bucket",
                        lambda name=None: _Bucket(name or export_store.bucket_name()))
    monkeypatch.setenv("GCS_EXPORTS_BUCKET", "the-bucket-configured-today")
    export_store.open_stream("exports/acct/2026/08/1-old.xlsx",
                             "the-bucket-it-was-written-to")
    assert seen["bucket"] == "the-bucket-it-was-written-to"


def test_a_number_called_in_both_directions_reports_both(corpus):
    """Taking whichever call landed first hid half the story on exactly the
    contacts that matter most — the ones we called who then called back."""
    from shared.db.database import get_db

    with get_db() as db:
        db.execute("UPDATE calls SET direction='inbound' WHERE id=?",
                   (corpus["rita2"],))
    blob, _ = _build(corpus, [corpus["rita1"], corpus["rita2"]])
    ws = _sheets(blob)["Numbers"]
    header = [c.value for c in ws[1]]
    col = header.index("Direction") + 1
    assert ws.cell(row=2, column=col).value == "inbound, outbound"
