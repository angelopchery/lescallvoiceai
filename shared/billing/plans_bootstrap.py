"""Create the gateway's Plan objects for the recurring tiers. Idempotent.

    python -m shared.billing.plans_bootstrap            # dry run, prints intent
    python -m shared.billing.plans_bootstrap --apply

READ THIS BEFORE RUNNING WITH --apply IN LIVE MODE
--------------------------------------------------
Razorpay Plans are IMMUTABLE FOREVER. There is no price edit, no delete. A
price change means creating a second plan id and migrating every existing
subscriber onto it by cancelling and re-authorising their mandate — i.e.
asking every paying customer to re-enter their card. So the number in
`plans.price_paise` must be the number you intend to charge for a long time.

Only the RECURRING tiers need a Plan:
  * Basic and Growth  -> gateway Plan + Subscription
  * Pilot and top-ups -> one-time Orders created per purchase, so their price
    lives only in our `plans` row and can be changed freely at any time.
  * Free / Enterprise -> never touch the gateway.

Prices in `plans` are GST-EXCLUSIVE; the gateway Plan is created at the GROSS
amount (base x 1.18) because that is what actually gets debited.
"""

from __future__ import annotations

import argparse
import logging
import sys

from shared.billing import razorpay_client as rz

log = logging.getLogger("billing.bootstrap")

RECURRING_CODES = ("basic", "growth")


def _id_column(mode: str) -> str:
    return "razorpay_plan_id_live" if mode == "live" else "razorpay_plan_id_test"


def bootstrap(apply: bool = False) -> int:
    from shared.db.database import get_db

    mode = rz.mode()
    column = _id_column(mode)
    if apply and not rz.configured():
        print("razorpay is not configured — set RAZORPAY_KEY_ID / "
              "RAZORPAY_KEY_SECRET in /etc/lescall/billing.env", file=sys.stderr)
        return 2

    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT code, name, price_paise, period, {column} AS plan_id "
            "FROM plans WHERE code = ANY(?) ORDER BY sort",
            (list(RECURRING_CODES),)).fetchall()]

    print(f"mode={mode}  column={column}")
    created = 0
    for row in rows:
        gross = rz.gross_paise(row["price_paise"])
        label = (f"  {row['code']:<10} base={row['price_paise'] / 100:>10,.2f} "
                 f"gross={gross / 100:>10,.2f} INR/month")
        if row["plan_id"]:
            print(f"{label}  ALREADY CREATED ({row['plan_id']}) — skipping")
            continue
        if not apply:
            print(f"{label}  would CREATE")
            created += 1
            continue

        result = rz.create_plan(
            name=f"LesCall {row['name']}", amount_paise=gross,
            period="monthly", interval=1,
            notes={"plan_code": row["code"], "base_paise": str(row["price_paise"])})
        plan_id = result.get("id")
        if not plan_id:
            print(f"{label}  FAILED — gateway returned no id: {result}",
                  file=sys.stderr)
            return 1
        with get_db() as db:
            # Guard against a concurrent run: only fill an id that is still
            # NULL, so a second bootstrap can never orphan the first's plan.
            db.execute(
                f"UPDATE plans SET {column}=?, updated_at=CURRENT_TIMESTAMP "
                f"WHERE code=? AND {column} IS NULL", (plan_id, row["code"]))
        print(f"{label}  CREATED {plan_id}")
        created += 1

    if not apply:
        print(f"\n{created} plan(s) would be created. Re-run with --apply.")
        if mode == "live":
            print("LIVE MODE: gateway plans are immutable once created. "
                  "Confirm the prices above are final.")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually create the plans (default is a dry run)")
    args = ap.parse_args()
    return bootstrap(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
