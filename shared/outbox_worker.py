"""Outbox delivery worker (Part B Phase 2) — thin CLI around shared.outbox.

Usage:
    python3 -m shared.outbox_worker --once              # one pass, print JSON
    python3 -m shared.outbox_worker --interval 30       # loop forever
    python3 -m shared.outbox_worker --once --db-url sqlite:///path/to.db

Loop mode sleeps `--interval` seconds between deliver_pending passes and logs
each summary. Ctrl-C (SIGINT) and SIGTERM exit cleanly.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

from shared.outbox import deliver_pending

log = logging.getLogger("lescall.outbox_worker")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shared.outbox_worker",
        description="Deliver pending outbox rows to the client ERP webhook.")
    parser.add_argument("--once", action="store_true",
                        help="run a single deliver pass and print the JSON summary")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="seconds between passes in loop mode (default 30)")
    parser.add_argument("--db-url", default=None,
                        help="database URL (default: LESCALL_DB_URL or the "
                             "shared sqlite file)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.once:
        print(json.dumps(deliver_pending(url=args.db_url), indent=2))
        return

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("outbox worker started (interval=%ss)", args.interval)
    try:
        while True:
            try:
                summary = deliver_pending(url=args.db_url)
                log.info("outbox pass: %s", json.dumps(summary))
            except Exception:
                log.exception("outbox pass failed; will retry next interval")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("outbox worker stopped")


if __name__ == "__main__":
    main()
