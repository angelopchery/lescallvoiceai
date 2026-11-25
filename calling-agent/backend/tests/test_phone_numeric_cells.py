"""A phone number that arrived as a spreadsheet NUMBER (RUNBOOK §58).

2026-09-25: a 20-row hiring list (.xlsx) was uploaded; openpyxl handed the
phone column back as floats, `str(9128692865.0)` is "9128692865.0", and the
digit stripper turned every number into an 11-digit one with a trailing 0.
`to_e164` then read them as international numbers, the campaign dialled
91286928650, 83097276130, … and the carrier answered UNALLOCATED_NUMBER
twenty times — shown to the operator as twenty people who "declined".

Three things are pinned here: the shared normaliser undoes float and
scientific-notation formatting (every write path and the dial itself go
through it), the xlsx reader never produces "…​.0" in the first place, and a
carrier saying "no such number" is reported as `invalid_number`, never as
`declined`.
"""
from __future__ import annotations

import io
import sys

import pytest

from conftest import REPO_ROOT

from shared.phone import digits_only, numeric_text, significant_digits, to_e164


@pytest.mark.parametrize("raw, e164", [
    ("9128692865.0", "+919128692865"),          # float-formatted cell text
    ("8309727613.00", "+918309727613"),
    ("9.128692865E9", "+919128692865"),         # Excel's display of the same float
    ("9.128692865E+09", "+919128692865"),
    ("919128692865.0", "+919128692865"),        # with country code AND a fraction
    ("9128692865", "+919128692865"),            # what a human types — unchanged
    ("+91 80659-53160", "+918065953160"),
    ("079-23251306", "+917923251306"),
    ("+18001021111", "+18001021111"),           # a real 11-digit international number
])
def test_to_e164_undoes_spreadsheet_number_formatting(raw, e164):
    assert to_e164(raw) == e164


def test_numeric_text_leaves_ordinary_numbers_alone():
    assert numeric_text("+91 80659-53160") == "+91 80659-53160"
    assert numeric_text("23250771/72") == "23250771/72"
    assert numeric_text("1.5") == "1.5"          # too short to be a phone; untouched
    assert digits_only("9128692865.0") == "9128692865"
    assert significant_digits("9128692865.0") == "9128692865"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    for p in (str(REPO_ROOT / "dashboard"), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import server as srv
    return srv


def test_xlsx_numeric_phone_cells_are_stored_dialable(server):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Phone", "Education"])
    ws.append(["Rinshika Dubey", 9128692865.0, "B Tech"])      # float cell
    ws.append(["Sunil Pattar", 8217451552, "BE"])              # int cell
    ws.append(["Typed", "+91 84313 80025", "B Tech"])          # text cell
    buf = io.BytesIO()
    wb.save(buf)
    rows, errors = server._parse_uploaded_file("candidates.xlsx", buf.getvalue())
    assert errors == 0
    phones = [r["phone"] for r in rows]
    assert phones == ["+919128692865", "+918217451552", "+918431380025"], phones
    assert server._xlsx_cell_text(9128692865.0) == "9128692865"
    assert server._xlsx_cell_text(None) == ""
    assert server._xlsx_cell_text(12.5) == "12.5"


def test_csv_float_formatted_phone_is_stored_dialable(server):
    csv = "Name,Phone\nA,9128692865.0\nB,9.128692865E9\n".encode()
    rows, _ = server._parse_uploaded_file("candidates.csv", csv)
    assert [r["phone"] for r in rows] == ["+919128692865", "+919128692865"]


@pytest.mark.parametrize("fs_out, status", [
    ("-ERR UNALLOCATED_NUMBER", "invalid_number"),
    ("-ERR INVALID_NUMBER_FORMAT", "invalid_number"),
    ("-ERR NO_ROUTE_DESTINATION", "invalid_number"),
    ("-ERR CALL_REJECTED", "declined"),
    ("-ERR NORMAL_CLEARING", "declined"),
    ("-ERR NO_ANSWER", "no_answer"),
    ("-ERR USER_BUSY", "busy"),
    ("+OK 1234-uuid", "answered"),
    ("-ERR something new", "failed"),
])
def test_a_wrong_number_is_not_a_person_declining(server, fs_out, status):
    assert server._parse_fs_result(fs_out, "") == status


def test_invalid_number_is_a_first_class_status(server):
    assert "invalid_number" in server.CALL_STATUSES
    assert server._fresh_campaign()["invalid_number"] == 0
