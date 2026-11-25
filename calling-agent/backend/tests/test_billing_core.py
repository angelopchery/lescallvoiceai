"""Billing: signatures, GST, periods, quota arithmetic.

Everything here is PURE — no database, no gateway, no HTTP — so it runs in the
normal suite. The database-backed lifecycle lives in
test_billing_lifecycle.py, which skips unless Postgres is reachable.

The signature tests are the most important ones in the file. Razorpay uses the
same HMAC construction with the OPPOSITE field order for its two flows, this
system runs both, and getting them the wrong way round either rejects every
genuine payment or — if you "fix" it by comparing against the id the client
echoed back — accepts forged ones.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from shared.billing import razorpay_client as rz  # noqa: E402
from shared.billing import receipts  # noqa: E402
from shared.billing.entitlements import (  # noqa: E402
    IST, as_dt, ist_month_bounds,
)
from shared.billing.errors import Decision, refusal_body  # noqa: E402

SECRET = "test_secret_do_not_use"


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
    monkeypatch.setenv("PLATFORM_GSTIN", "24AAACC1206D1ZM")   # Gujarat
    monkeypatch.setenv("PLATFORM_STATE_CODE", "24")
    monkeypatch.setenv("PLATFORM_LEGAL_NAME", "Test Co")


def _sign(msg: str) -> str:
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


# ── signatures: the field-order trap ────────────────────────────────────────

def test_order_signature_verifies_with_order_id_first():
    sig = _sign("order_A|pay_B")
    assert rz.verify_order_signature(
        order_id="order_A", payment_id="pay_B", signature=sig)


def test_subscription_signature_verifies_with_payment_id_first():
    sig = _sign("pay_B|sub_C")
    assert rz.verify_subscription_signature(
        subscription_id="sub_C", payment_id="pay_B", signature=sig)


def test_order_signature_FAILS_subscription_verification():
    """The single easiest way to break both flows at once."""
    sig = _sign("order_A|pay_B")
    assert not rz.verify_subscription_signature(
        subscription_id="order_A", payment_id="pay_B", signature=sig)


def test_subscription_signature_FAILS_order_verification():
    sig = _sign("pay_B|sub_C")
    assert not rz.verify_order_signature(
        order_id="sub_C", payment_id="pay_B", signature=sig)


def test_signature_rejects_tampered_ids():
    sig = _sign("order_A|pay_B")
    assert not rz.verify_order_signature(
        order_id="order_ATTACKER", payment_id="pay_B", signature=sig)
    assert not rz.verify_order_signature(
        order_id="order_A", payment_id="pay_ATTACKER", signature=sig)


def test_signature_rejects_empty_inputs():
    assert not rz.verify_order_signature(order_id="", payment_id="p", signature="s")
    assert not rz.verify_order_signature(order_id="o", payment_id="p", signature="")


def test_webhook_signature_is_over_raw_bytes(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec")
    body = b'{"event":"order.paid","payload":{}}'
    good = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert rz.verify_webhook_signature(body, good)
    # A single byte changed anywhere must fail — this is why the handler keeps
    # the raw body instead of re-serialising the parsed JSON.
    assert not rz.verify_webhook_signature(body + b" ", good)
    assert not rz.verify_webhook_signature(body, good[:-1] + "0")


def test_webhook_accepts_previous_secret_during_rotation(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "new")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET_PREVIOUS", "old")
    body = b'{"event":"x"}'
    assert rz.verify_webhook_signature(
        body, hmac.new(b"old", body, hashlib.sha256).hexdigest())
    assert rz.verify_webhook_signature(
        body, hmac.new(b"new", body, hashlib.sha256).hexdigest())


# ── GST arithmetic ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("base,gross", [
    (299900, 353882),      # Pilot   ₹2,999
    (1499900, 1769882),    # Basic   ₹14,999
    (4999900, 5899882),    # Growth  ₹49,999
])
def test_gross_is_base_plus_18_percent(base, gross):
    assert rz.gross_paise(base) == gross


def test_both_recurring_tiers_are_above_the_afa_ceiling():
    """Not a preference — a fact that drives the checkout copy.

    RBI's AFA-free e-mandate ceiling is ₹15,000 per debit, GST included. At the
    published prices BOTH Basic and Growth exceed it, so card/UPI mandates will
    prompt the customer every month and eMandate is the method to prefer. If a
    price ever drops below the ceiling this test should be updated, not
    deleted — the copy depends on it.
    """
    assert rz.gross_paise(1499900) > 1_500_000
    assert rz.gross_paise(4999900) > 1_500_000
    assert rz.gross_paise(299900) < 1_500_000   # Pilot is one-time anyway


def test_intra_state_supply_splits_cgst_sgst():
    tax = receipts.split_tax(299900, "24AAACC1206D1ZM")   # same state as platform
    assert not tax["inter_state"]
    assert tax["igst_paise"] == 0
    assert tax["cgst_paise"] + tax["sgst_paise"] == tax["tax_paise"]
    assert tax["total_paise"] == 353882


def test_inter_state_supply_uses_igst():
    tax = receipts.split_tax(299900, "27AAPFU0939F1ZV")   # Maharashtra
    assert tax["inter_state"]
    assert tax["igst_paise"] == tax["tax_paise"]
    assert tax["cgst_paise"] == tax["sgst_paise"] == 0


def test_b2c_no_gstin_is_intra_state_at_platform_state():
    tax = receipts.split_tax(100000, "")
    assert not tax["inter_state"]
    assert tax["place_of_supply"].startswith("24")


def test_odd_paisa_never_loses_money_in_the_split():
    """CGST+SGST must sum to the exact tax, including on odd amounts."""
    for base in (1, 3, 7, 99, 12345, 299900, 4999900):
        tax = receipts.split_tax(base, "24AAACC1206D1ZM")
        assert tax["cgst_paise"] + tax["sgst_paise"] == tax["tax_paise"]
        assert tax["base_paise"] + tax["tax_paise"] == tax["total_paise"]


# ── GSTIN validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("gstin", [
    "27AAPFU0939F1ZV", "24AAACC1206D1ZM", "29AAGCB7383J1Z4",
])
def test_valid_gstins_pass_the_checksum(gstin):
    ok, norm = receipts.validate_gstin(gstin)
    assert ok and norm == gstin


def test_gstin_checksum_catches_a_single_character_typo():
    ok, msg = receipts.validate_gstin("27AAPFU0939F1ZX")
    assert not ok and "check digit" in msg


def test_gstin_is_normalized_to_uppercase():
    ok, norm = receipts.validate_gstin("  27aapfu0939f1zv ")
    assert ok and norm == "27AAPFU0939F1ZV"


def test_empty_gstin_is_valid_and_means_b2c():
    ok, norm = receipts.validate_gstin("")
    assert ok and norm == ""


def test_bad_state_code_is_rejected():
    ok, msg = receipts.validate_gstin("99AAPFU0939F1ZV")
    assert not ok


def test_state_is_derived_from_gstin_not_supplied():
    code, name = receipts.state_of("27AAPFU0939F1ZV")
    assert (code, name) == ("27", "Maharashtra")


# ── receipt numbering ───────────────────────────────────────────────────────

def test_financial_year_rolls_over_on_1_april_IST():
    # 23:00 UTC on 31 March is already 04:30 on 1 April in India, so it belongs
    # to the NEW year's series.
    assert receipts.financial_year(
        dt.datetime(2026, 3, 31, 23, 0, tzinfo=dt.timezone.utc)) == "26-27"
    assert receipts.financial_year(
        dt.datetime(2026, 3, 31, 10, 0, tzinfo=dt.timezone.utc)) == "25-26"
    assert receipts.financial_year(
        dt.datetime(2026, 4, 1, 6, 0, tzinfo=dt.timezone.utc)) == "26-27"


# ── period arithmetic ───────────────────────────────────────────────────────

def test_ist_month_bounds_are_a_calendar_month_in_india():
    start, end = ist_month_bounds(dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.timezone.utc))
    assert start.astimezone(IST).day == 1
    assert start.astimezone(IST).month == 7
    assert end.astimezone(IST).month == 8
    assert end.astimezone(IST).day == 1


def test_ist_month_bounds_handle_february_and_december():
    for month, nxt in ((2, 3), (12, 1)):
        at = dt.datetime(2026, month, 10, tzinfo=dt.timezone.utc)
        start, end = ist_month_bounds(at)
        assert start.astimezone(IST).month == month
        assert end.astimezone(IST).month == nxt


def test_a_call_just_after_ist_midnight_belongs_to_the_new_month():
    """18:31 UTC on 31 July is 00:01 IST on 1 August."""
    start, _ = ist_month_bounds(dt.datetime(2026, 7, 31, 18, 31, tzinfo=dt.timezone.utc))
    assert start.astimezone(IST).month == 8


def test_as_dt_coerces_the_string_timestamps_postgres_returns():
    """Timestamps come back from this codebase's DB layer as STRINGS."""
    assert as_dt("2026-07-31 11:46:48.350512+00").year == 2026
    assert as_dt("2026-07-31T11:46:48Z").tzinfo is not None
    # A naive string is treated as UTC rather than local — being wrong by the
    # host's offset would silently shift every billing period.
    assert as_dt("2026-07-31 11:46:48").tzinfo == dt.timezone.utc
    assert as_dt(None) is None and as_dt("") is None and as_dt("nonsense") is None


# ── refusal shape ───────────────────────────────────────────────────────────

def test_refusal_carries_both_dialects_and_never_the_duplicate_flag():
    body = refusal_body("out of minutes", "quota_minutes")
    # `reason` is what the Outbound UI reads; `detail` is what FastAPI clients
    # read; `code` is what the SPA switches on.
    assert body["reason"] == body["detail"] == "out of minutes"
    assert body["code"] == "quota_minutes"
    assert body["upgrade"] == "/billing"
    # duplicate:true is the 409 duplicate-dial shape, and ?force=1 bypasses
    # that. Billing must never be force-bypassable.
    assert "duplicate" not in body


def test_refusal_strips_a_duplicate_flag_even_if_passed_in():
    assert "duplicate" not in refusal_body("x", "quota_minutes", duplicate=True)


def test_decision_ok_sets_reservation_as_a_field_not_detail():
    """Regression: passed as **detail, `.reservation` stayed empty and every
    release became a no-op, so admissions accumulated until they aged out."""
    d = Decision.ok(reservation="dial:abc")
    assert d.reservation == "dial:abc"
    assert bool(d) is True


def test_decision_deny_raises_a_402_http_error():
    from fastapi import HTTPException
    d = Decision.deny("quota_minutes", "no minutes")
    assert bool(d) is False
    with pytest.raises(HTTPException) as exc:
        d.raise_for_http()
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "quota_minutes"


# ── gateway degradation ─────────────────────────────────────────────────────

def test_gateway_reports_unconfigured_without_keys(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    assert not rz.configured()


def test_gateway_calls_raise_a_distinct_unavailable_error(monkeypatch):
    """503 'payments are not set up yet' and 'your payment failed' are very
    different things to show a customer, so they are different exceptions."""
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(rz.GatewayUnavailable):
        rz.create_order(amount_paise=100, receipt="r")
    assert issubclass(rz.GatewayUnavailable, rz.GatewayError)
