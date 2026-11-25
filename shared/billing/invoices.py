"""Manual invoices — money collected outside the payment gateway.

Cash, NEFT/RTGS, a cheque, or an account put on credit. Each one produces a
real GST tax invoice, numbered from the same consecutive series as a card
payment, visible to the customer in their own Billing page.

The rules this module enforces, and why each one exists:

* **An invoice is numbered when ISSUED, not when paid.** GST raises the
  invoice at supply. So an unpaid invoice takes the next number in the FY
  series immediately, and if it is never paid it is cancelled with a credit
  note — never deleted. A gap in the series is an audit finding.

* **Issuing activates the plan**, paid or not. Extending credit means the
  customer gets the service now; the balance sits as outstanding.

* **The invoice number comes from the ISSUE date's financial year**, not
  today's. An invoice dated 28 March belongs to the old FY's series.
  Back-dating across a year boundary is refused outright — it would insert a
  number into a series that has already been filed.

* **The plan grant is written as an `overrides` entry**, never as a bare
  `plan_code`. Entitlements are derived state: `resolve_entitlements()`
  rebuilds the row from the subscriptions history, and a bare plan_code is
  silently discarded the next time anything touches the account. (Learned the
  hard way — migration 0014.)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets

from shared.billing import razorpay_client as rz
from shared.billing import receipts
from shared.billing.entitlements import as_dt, now_utc, resolve_entitlements

log = logging.getLogger("billing.invoices")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# How the money arrived. `credit` is the odd one out: it is the ABSENCE of a
# payment, so it maps to status='due' rather than a method.
METHODS = {
    "cash": "Cash",
    "neft_rtgs": "NEFT / RTGS",
    "imps": "IMPS",
    "upi": "UPI",
    "cheque": "Cheque",
    "bank_transfer": "Bank transfer",
    "other": "Other",
    "credit": "Not yet received (on credit)",
}

DEFAULT_CREDIT_DAYS = 15


class InvoiceError(ValueError):
    """A refusal an operator should read and act on."""


def _month_add(when: dt.datetime, months: int) -> dt.datetime:
    """Add whole months, clamping the day (31 Jan + 1 month -> 28/29 Feb)."""
    month = when.month - 1 + months
    year = when.year + month // 12
    month = month % 12 + 1
    day = min(when.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0)
                         else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return when.replace(year=year, month=month, day=day)


def issue(*, owner: str, plan_code: str, base_paise: int, method: str,
          issued_at: dt.datetime | None = None, period_months: int = 1,
          reference: str = "", note: str = "", actor: str = "admin",
          grant_plan: bool = True) -> dict:
    """Issue a manual tax invoice and (by default) activate the plan.

    Returns the created payment row. Raises InvoiceError with a readable
    message for anything an operator can fix.
    """
    from shared.db.database import get_db

    owner = (owner or "").strip().lower()
    if not owner:
        raise InvoiceError("An account is required.")
    if method not in METHODS:
        raise InvoiceError(f"Unknown payment method {method!r}.")
    try:
        base_paise = int(base_paise)
    except (TypeError, ValueError):
        raise InvoiceError("Amount must be a whole number of paise.")
    if base_paise <= 0:
        raise InvoiceError("Amount must be greater than zero.")
    if base_paise > 100_00_00_000:          # ₹1 crore, a sanity ceiling
        raise InvoiceError("Amount looks wrong — over ₹1,00,00,000.")
    if not 1 <= int(period_months) <= 60:
        raise InvoiceError("Billing period must be between 1 and 60 months.")

    issued = issued_at or now_utc()
    today = now_utc()
    if issued > today + dt.timedelta(days=1):
        raise InvoiceError("An invoice cannot be dated in the future.")
    # The number comes from the issue date's FY. Crossing a year boundary would
    # slot a number into a series that has already been filed.
    if receipts.financial_year(issued) != receipts.financial_year(today):
        raise InvoiceError(
            f"That date falls in financial year "
            f"{receipts.financial_year(issued)}, but the current series is "
            f"{receipts.financial_year(today)}. Invoices cannot be back-dated "
            f"across a financial year.")

    # Refuse to issue without a supplier identity.
    #
    # A document with no GSTIN, legal name or place of supply is not a tax
    # invoice — it is a number burned out of the GST series for something the
    # customer cannot claim credit against and we cannot file. Three such
    # invoices reached a live tenant's Billing page during this feature's own
    # testing before this check existed; they had to be deleted and the series
    # reset. Better to refuse and say exactly which field is missing.
    ready, detail = receipts.platform_ready()
    if not ready:
        raise InvoiceError(
            f"Cannot issue a tax invoice yet: {detail}. Set PLATFORM_GSTIN, "
            f"PLATFORM_LEGAL_NAME, PLATFORM_ADDRESS and PLATFORM_STATE_CODE in "
            f"/etc/lescall/billing.env, then `docker compose up -d api workers`.")

    unpaid = method == "credit"
    status = "due" if unpaid else "captured"

    with get_db() as db:
        plan = db.execute("SELECT code, name FROM plans WHERE code=?",
                          (plan_code,)).fetchone()
        if plan is None:
            raise InvoiceError(f"Unknown plan {plan_code!r}.")

        snapshot = receipts.build_snapshot(owner, base_paise, dict(plan))
        tax = snapshot["tax"]
        number = receipts.allocate_number(db, issued)
        period_start = issued
        period_end = _month_add(issued, int(period_months))

        row = db.execute(
            "INSERT INTO payments (owner, source, purpose, amount_paise, "
            "base_paise, tax_paise, status, method, period_start, period_end, "
            "receipt_number, billing_snapshot, notes, issued_at, due_date, "
            "paid_at, payment_reference, created_by, razorpay_payment_id) "
            "VALUES (?, 'manual', 'manual', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "RETURNING id",
            (owner, tax["total_paise"], tax["base_paise"], tax["tax_paise"],
             status, None if unpaid else method,
             period_start.isoformat(), period_end.isoformat(), number,
             json.dumps(snapshot, ensure_ascii=False), note or None,
             issued.isoformat(),
             (issued + dt.timedelta(days=DEFAULT_CREDIT_DAYS)).isoformat()
             if unpaid else None,
             None if unpaid else issued.isoformat(),
             reference or None, actor,
             # payments.razorpay_payment_id is UNIQUE and is the idempotency
             # key for gateway events. A manual invoice has no gateway id, and
             # NULL would let unlimited NULLs through — so it gets a synthetic
             # one, which also keeps the "one row per payment" rule uniform.
             f"manual-{secrets.token_hex(12)}")).fetchone()
        payment_id = int(row["id"])

        if grant_plan:
            _grant(db, owner, plan_code, period_start, period_end, actor)

    if grant_plan:
        resolve_entitlements(owner, actor=f"invoice:{actor}")

    log.info("[billing] manual invoice %s issued for %s: %s %s (%s) plan=%s",
             number, owner, tax["total_paise"], status, method, plan_code)
    return get(payment_id)


def _grant(db, owner: str, plan_code: str, start: dt.datetime,
           end: dt.datetime, actor: str) -> None:
    """Put the tenant on the plan for the invoiced period.

    Written into `overrides` because entitlements are DERIVED: a bare
    plan_code on the row does not survive the next resolve.
    """
    prior = db.execute("SELECT overrides FROM entitlements WHERE owner=?",
                       (owner,)).fetchone()
    overrides = {}
    if prior and prior["overrides"]:
        try:
            overrides = json.loads(prior["overrides"]) or {}
        except Exception:
            overrides = {}
    overrides.update({
        "plan_code": plan_code,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    })
    db.execute(
        "INSERT INTO entitlements (owner, plan_code, overrides, updated_by, "
        "updated_at) VALUES (?,?,?,?, CURRENT_TIMESTAMP) "
        "ON CONFLICT (owner) DO UPDATE SET plan_code=excluded.plan_code, "
        "overrides=excluded.overrides, updated_by=excluded.updated_by, "
        "updated_at=CURRENT_TIMESTAMP",
        (owner, plan_code, json.dumps(overrides), f"invoice:{actor}"))


def mark_paid(payment_id: int, *, method: str, paid_at: dt.datetime | None = None,
              reference: str = "", actor: str = "admin") -> dict:
    """Settle an outstanding invoice. The invoice number never changes."""
    from shared.db.database import get_db

    if method not in METHODS or method == "credit":
        raise InvoiceError("Choose how the money actually arrived.")
    paid = paid_at or now_utc()
    with get_db() as db:
        row = db.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if row is None:
            raise InvoiceError("No such invoice.")
        row = dict(row)
        if row["status"] == "void":
            raise InvoiceError("That invoice was cancelled.")
        if row["status"] != "due":
            raise InvoiceError("That invoice is not outstanding.")
        db.execute(
            "UPDATE payments SET status='captured', method=?, paid_at=?, "
            "payment_reference=COALESCE(NULLIF(?,''), payment_reference) "
            "WHERE id=? AND status='due'",
            (method, paid.isoformat(), reference, payment_id))
    log.info("[billing] invoice %s marked paid (%s) by %s",
             row["receipt_number"], method, actor)
    return get(payment_id)


def void(payment_id: int, *, reason: str, actor: str = "admin") -> dict:
    """Cancel an invoice with a credit note.

    Deleting is not an option: the number is part of a filed consecutive
    series, so the only lawful reversal is a credit note against it.
    """
    from shared.db.database import get_db

    reason = (reason or "").strip()
    if len(reason) < 10:
        raise InvoiceError("A reason of at least 10 characters is required — "
                           "it is printed on the credit note.")
    with get_db() as db:
        row = db.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if row is None:
            raise InvoiceError("No such invoice.")
        row = dict(row)
        if row["status"] == "void":
            raise InvoiceError("That invoice is already cancelled.")
        if row["source"] != "manual":
            raise InvoiceError("Gateway payments are reversed by refunding "
                               "them, not by voiding the invoice.")
        credit_number = row.get("credit_note_number") or receipts.allocate_number(
            db, now_utc(), credit_note=True)
        db.execute(
            "UPDATE payments SET status='void', voided_at=CURRENT_TIMESTAMP, "
            "void_reason=?, credit_note_number=COALESCE(credit_note_number, ?) "
            "WHERE id=?", (reason, credit_number, payment_id))
    resolve_entitlements(row["owner"], actor=f"invoice-void:{actor}")
    log.info("[billing] invoice %s VOIDED by %s (credit note %s): %s",
             row["receipt_number"], actor, credit_number, reason)
    return get(payment_id)


def get(payment_id: int) -> dict:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    return dict(row) if row else {}


def outstanding(owner: str = "") -> list[dict]:
    """Unpaid invoices, oldest first. Whole platform when owner is blank."""
    from shared.db.database import get_db
    sql = ("SELECT * FROM payments WHERE status='due'"
           + (" AND owner=?" if owner else "")
           + " ORDER BY due_date NULLS LAST, id")
    with get_db() as db:
        rows = db.execute(sql, (owner,) if owner else ()).fetchall()
    out = []
    now = now_utc()
    for r in rows:
        d = dict(r)
        due = as_dt(d.get("due_date"))
        d["overdue"] = bool(due and due < now)
        d["days_overdue"] = (now - due).days if due and due < now else 0
        out.append(d)
    return out


def outstanding_total(owner: str = "") -> int:
    return sum(int(r["amount_paise"] or 0) for r in outstanding(owner))


def preview(plan_code: str, base_paise: int | None = None,
            owner: str = "") -> dict:
    """What the invoice will say, before it is issued — for the live form."""
    from shared.db.database import get_db
    with get_db() as db:
        plan = db.execute("SELECT code, name, price_paise FROM plans WHERE code=?",
                          (plan_code,)).fetchone()
    if plan is None:
        raise InvoiceError(f"Unknown plan {plan_code!r}.")
    base = int(base_paise if base_paise is not None else plan["price_paise"])
    profile = receipts.get_billing_profile(owner) if owner else {}
    tax = receipts.split_tax(base, profile.get("gstin", ""))
    return {"plan": dict(plan), "tax": tax,
            "gateway_equivalent": rz.gross_paise(int(plan["price_paise"]))}
