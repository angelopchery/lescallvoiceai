"""GST tax invoices.

Razorpay's own auto-invoice is a PAYMENT RECEIPT, not a tax invoice: it has no
customer GSTIN, no SAC code and no CGST/SGST split. At Rs 14,999 and Rs 49,999
a month the customers are businesses who will claim input credit, so we issue
the tax invoice ourselves.

Three things here are non-obvious and all three are correctness-critical:

  * The invoice NUMBER must be a consecutive series per financial year. A
    MAX(seq)+1 read races between the two processes that insert payments (the
    api webhook and the admin plane's manual entry), and a gap or duplicate in
    an invoice series is an audit finding — so allocation is
    `SELECT ... FOR UPDATE` on a counter row, inside the payment's own
    transaction.

  * The place of supply is DERIVED FROM THE GSTIN, never accepted as a
    separate field. A customer who types GSTIN 24XXXX (Gujarat) and state
    "Maharashtra" would otherwise get IGST on an intra-state supply, which is
    the customer's problem at assessment time and ours at audit.

  * The customer's billing details are SNAPSHOTTED onto the payment row at
    issue. An invoice is a legal document; editing a profile afterwards must
    not retroactively rewrite one that has already been issued.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re

log = logging.getLogger("billing.receipts")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GST_RATE = 0.18
# 998314 — "Information technology (IT) design and development services".
SAC_CODE = "998314"

# GSTIN: 2-digit state code, 10-char PAN, 1 entity digit, 'Z', 1 check char.
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

STATE_NAMES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}


# ---------------------------------------------------------------------------
# GSTIN
# ---------------------------------------------------------------------------

def gstin_checksum_ok(gstin: str) -> bool:
    """The mod-36 check character.

    Each of the first 14 characters is weighted alternately 1 and 2 over the
    36-character alphabet; the digit-sum of each product is accumulated, and
    the check character is whatever brings the total to a multiple of 36.
    A format-only check accepts a typo'd GSTIN, and a wrong GSTIN on an issued
    invoice means the customer cannot claim the credit.
    """
    if len(gstin) != 15:
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        pos = _GSTIN_ALPHABET.find(ch)
        if pos < 0:
            return False
        product = pos * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    expected = _GSTIN_ALPHABET[(36 - total % 36) % 36]
    return expected == gstin[14]


def validate_gstin(raw: str) -> tuple[bool, str]:
    """(valid, normalized_or_error). Empty input is VALID and means B2C."""
    gstin = (raw or "").strip().upper().replace(" ", "")
    if not gstin:
        return True, ""
    if not _GSTIN_RE.match(gstin):
        return False, "GSTIN must be 15 characters, e.g. 24AAAAA0000A1Z5"
    if gstin[:2] not in STATE_NAMES:
        return False, f"'{gstin[:2]}' is not a valid GST state code"
    if not gstin_checksum_ok(gstin):
        return False, "GSTIN check digit is wrong — please re-check the number"
    return True, gstin


def state_of(gstin: str) -> tuple[str, str]:
    """(code, name) derived from the GSTIN. Never taken as a separate input."""
    code = (gstin or "")[:2]
    return code, STATE_NAMES.get(code, "")


# ---------------------------------------------------------------------------
# platform identity (the supplier)
# ---------------------------------------------------------------------------

def platform_profile() -> dict:
    return {
        "legal_name": os.environ.get("PLATFORM_LEGAL_NAME", "").strip(),
        "gstin": os.environ.get("PLATFORM_GSTIN", "").strip().upper(),
        "address": os.environ.get("PLATFORM_ADDRESS", "").strip(),
        "state_code": os.environ.get("PLATFORM_STATE_CODE", "").strip(),
    }


def platform_ready() -> tuple[bool, str]:
    p = platform_profile()
    if not p["legal_name"]:
        return False, "PLATFORM_LEGAL_NAME is unset"
    ok, norm = validate_gstin(p["gstin"])
    if not p["gstin"]:
        return False, "PLATFORM_GSTIN is unset"
    if not ok:
        return False, f"PLATFORM_GSTIN invalid: {norm}"
    if p["state_code"] not in STATE_NAMES:
        return False, "PLATFORM_STATE_CODE is unset or not a GST state code"
    return True, ""


# ---------------------------------------------------------------------------
# financial year + serial allocation
# ---------------------------------------------------------------------------

def financial_year(at: dt.datetime) -> str:
    """Indian FY label for a UTC instant, evaluated in IST: Apr 1 - Mar 31.

    Evaluated in IST on purpose. A payment captured at 23:00 UTC on 31 March
    is already 04:30 on 1 April in India, and belongs to the NEW year's series.
    """
    local = at.astimezone(IST)
    start_year = local.year if local.month >= 4 else local.year - 1
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def allocate_number(db, at: dt.datetime, credit_note: bool = False) -> str:
    """Next invoice number in the FY series. MUST run inside the caller's
    transaction, alongside the payments INSERT.

    `FOR UPDATE` serialises the two writers. Without it, the api webhook and
    an admin manual payment landing in the same millisecond both read the same
    last_seq and both issue e.g. LC-25-26-00042.
    """
    fy = financial_year(at)
    column = "last_credit_seq" if credit_note else "last_seq"
    db.execute(
        "INSERT INTO receipt_counters (fy) VALUES (?) ON CONFLICT (fy) DO NOTHING",
        (fy,))
    row = db.execute(
        f"SELECT {column} AS seq FROM receipt_counters WHERE fy=? FOR UPDATE",
        (fy,)).fetchone()
    seq = int((row or {}).get("seq") or 0) + 1
    db.execute(f"UPDATE receipt_counters SET {column}=? WHERE fy=?", (seq, fy))
    prefix = "LCCN" if credit_note else "LC"
    return f"{prefix}-{fy}-{seq:05d}"


# ---------------------------------------------------------------------------
# the tax computation
# ---------------------------------------------------------------------------

def split_tax(base_paise: int, customer_gstin: str = "") -> dict:
    """CGST+SGST for an intra-state supply, IGST for inter-state.

    Place of supply comes from the customer's GSTIN state; with no GSTIN the
    supply is B2C and the place of supply is the platform's own state, so it
    is intra-state (CGST+SGST).
    """
    base = int(base_paise)
    tax_total = int(round(base * GST_RATE))
    platform_state = platform_profile()["state_code"]
    cust_state, cust_state_name = state_of(customer_gstin)
    inter_state = bool(cust_state) and bool(platform_state) and cust_state != platform_state

    if inter_state:
        split = {"igst_paise": tax_total, "cgst_paise": 0, "sgst_paise": 0}
    else:
        half = tax_total // 2
        # The odd paisa goes to CGST, so the two halves always sum to the total.
        split = {"igst_paise": 0, "cgst_paise": tax_total - half, "sgst_paise": half}

    return {
        "base_paise": base,
        "tax_paise": tax_total,
        "total_paise": base + tax_total,
        "rate": GST_RATE,
        "inter_state": inter_state,
        "place_of_supply": (f"{cust_state}-{cust_state_name}" if cust_state
                            else f"{platform_state}-{STATE_NAMES.get(platform_state, '')}"),
        "sac": SAC_CODE,
        **split,
    }


# ---------------------------------------------------------------------------
# customer profile
# ---------------------------------------------------------------------------

def get_billing_profile(owner: str) -> dict:
    from shared.db.database import get_db
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE owner=? AND key='billing_profile'",
                (owner,)).fetchone()
        if row:
            data = json.loads(row["value"]) or {}
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log.warning("billing profile read failed for %s: %r", owner, exc)
    return {}


def save_billing_profile(owner: str, profile: dict) -> dict:
    """Validate and store. GSTIN is validated server-side; `state` is derived,
    never accepted from the caller."""
    ok, gstin = validate_gstin(str(profile.get("gstin") or ""))
    if not ok:
        raise ValueError(gstin)
    code, name = state_of(gstin)
    clean = {
        "legal_name": str(profile.get("legal_name") or "").strip()[:200],
        "gstin": gstin,
        "state_code": code,
        "state_name": name,
        "address": str(profile.get("address") or "").strip()[:500],
        "email": str(profile.get("email") or "").strip()[:254],
        "phone": str(profile.get("phone") or "").strip()[:20],
    }
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (owner, key, value, updated_at) "
            "VALUES (?, 'billing_profile', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (owner, key) DO UPDATE SET value=excluded.value, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner, json.dumps(clean, ensure_ascii=False)))
    return clean


def build_snapshot(owner: str, base_paise: int, plan: dict | None = None) -> dict:
    """Everything the invoice needs, frozen at issue time.

    `plan` is captured too so the invoice line names what was sold even after
    the catalogue is renamed or repriced.
    """
    profile = get_billing_profile(owner)
    tax = split_tax(base_paise, profile.get("gstin", ""))
    return {
        "supplier": platform_profile(),
        "customer": {**profile, "owner": owner,
                     "b2b": bool(profile.get("gstin"))},
        "plan": dict(plan) if plan else {},
        "tax": tax,
        "issued_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def render(payment: dict) -> dict:
    """A payment row + its snapshot, shaped for the printable receipt view.

    Reads the SNAPSHOT, not the live profile — that is the whole point of
    having taken one.
    """
    try:
        snapshot = json.loads(payment.get("billing_snapshot") or "{}") or {}
    except Exception:
        snapshot = {}
    tax = snapshot.get("tax") or split_tax(int(payment.get("base_paise") or 0))
    return {
        "number": payment.get("receipt_number"),
        "credit_note_number": payment.get("credit_note_number"),
        # The operator-set invoice date wins over the row's insert time: a
        # payment received last Tuesday is invoiced as last Tuesday.
        "issued_at": (payment.get("issued_at") or snapshot.get("issued_at")
                      or payment.get("created_at")),
        "due_date": payment.get("due_date"),
        "paid_at": payment.get("paid_at"),
        "payment_reference": payment.get("payment_reference"),
        "source": payment.get("source"),
        "void_reason": payment.get("void_reason"),
        "status": payment.get("status"),
        "method": payment.get("method"),
        "gateway_payment_id": payment.get("razorpay_payment_id"),
        "period_start": payment.get("period_start"),
        "period_end": payment.get("period_end"),
        "purpose": payment.get("purpose"),
        "supplier": snapshot.get("supplier") or platform_profile(),
        "customer": snapshot.get("customer") or {"owner": payment.get("owner")},
        "line_items": [{
            "description": _describe(payment),
            "sac": tax.get("sac", SAC_CODE),
            "amount_paise": tax.get("base_paise", payment.get("base_paise") or 0),
        }],
        "tax": tax,
        "amount_paise": payment.get("amount_paise"),
        "refunded_paise": payment.get("refunded_paise") or 0,
    }


def _describe(payment: dict) -> str:
    """The invoice line. Names the PLAN actually sold.

    A manual invoice used to read "LesCall Enterprise" whatever plan it was
    for, because manual and Enterprise were conflated. The plan is recorded in
    the snapshot at issue, so the line can say what the customer actually
    bought.
    """
    purpose = payment.get("purpose") or "subscription"
    if purpose == "topup":
        return "LesCall — additional call minutes"
    if purpose == "pilot":
        return "LesCall Pilot — 7-day trial (500 minutes)"

    plan_name = ""
    try:
        snap = json.loads(payment.get("billing_snapshot") or "{}") or {}
        plan_name = str((snap.get("plan") or {}).get("name") or "")
    except Exception:
        plan_name = ""
    period = ""
    if payment.get("period_start") and payment.get("period_end"):
        period = (f" ({str(payment['period_start'])[:10]} to "
                  f"{str(payment['period_end'])[:10]})")
    if plan_name:
        return f"LesCall {plan_name} — AI calling subscription{period}"
    return f"LesCall — AI calling subscription{period}"
