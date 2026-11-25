"""Sentinel GCS checks: the write-failure classifier and the recsync stall rule.

2026-09-16: the project's billing account went delinquent, every GCS WRITE
returned 403 for three days, and the sentinel's hourly probe — a one-object
LIST — passed throughout (RUNBOOK §59). These are the pure parts of the fix;
the real write probe needs a bucket and runs live.
"""
from __future__ import annotations

from conftest import REPO_ROOT  # noqa: F401 — puts the repo root on sys.path

from shared.sentinel import gcs_failure_kind, recsync_stuck

BILLING_403 = (
    "403 POST https://storage.googleapis.com/upload/storage/v1/b/"
    "lescall-recordings/o?uploadType=multipart: The billing account for the "
    "owning project is disabled in state delinquent")


# --- classifier -------------------------------------------------------------

def test_billing_delinquency_is_its_own_kind():
    assert gcs_failure_kind(Exception(BILLING_403)) == "billing"
    assert gcs_failure_kind(BILLING_403) == "billing"


def test_billing_wins_over_the_403_it_rides_in():
    # The delinquency message IS a 403 — it must never be read as auth.
    assert "403" in BILLING_403
    assert gcs_failure_kind(BILLING_403) == "billing"


def test_permission_denied_is_auth():
    msg = ("403 GET https://storage.googleapis.com/storage/v1/b/x: "
           "lescall-recsync@lescall.iam.gserviceaccount.com does not have "
           "storage.objects.create access")
    assert gcs_failure_kind(msg) == "auth"


def test_refresh_error_is_auth():
    class RefreshError(Exception):
        pass
    exc = RefreshError("Reauthentication is needed")
    assert gcs_failure_kind(f"{type(exc).__name__}: {exc}") == "auth"


def test_network_timeout_is_other():
    assert gcs_failure_kind("ReadTimeout: HTTPSConnectionPool timed out") == "other"


# --- recsync stall rule -----------------------------------------------------

def _line(uploaded: int, failed: int) -> str:
    return (f"2026-09-26 03:13:00,441 recording_storage INFO sync complete: "
            f"uploaded={uploaded} adopted=0 failed={failed} local_deleted=0 "
            f"exports_deleted=0\n")


NOISE = ("2026-09-26 03:00:47,309 billing.sync INFO billing sync: {'x': 0}\n"
         "2026-09-26 03:01:00,000 usage_metering INFO reconcile: {'y': 0}\n")


def test_two_failing_passes_are_stuck():
    log = NOISE + _line(0, 19) + NOISE + _line(0, 19)
    assert recsync_stuck(log, passes=2) == (19, 2)


def test_one_failing_pass_is_transient():
    log = _line(0, 0) + _line(0, 1)
    assert recsync_stuck(log, passes=2) is None


def test_progress_with_failures_is_not_a_stall():
    # The 2026-09-19 recovery pass: 17 landed, 1 did not. Not a page.
    log = _line(0, 19) + _line(17, 1)
    assert recsync_stuck(log, passes=2) is None


def test_recovery_clears_it():
    log = _line(0, 19) + _line(0, 19) + _line(0, 0)
    assert recsync_stuck(log, passes=2) is None


def test_only_the_tail_counts():
    log = _line(0, 5) + _line(0, 5) + _line(0, 0) + _line(0, 5)
    assert recsync_stuck(log, passes=2) is None


def test_needs_enough_history():
    assert recsync_stuck(_line(0, 19), passes=2) is None
    assert recsync_stuck("", passes=2) is None


def test_reports_the_latest_count():
    log = _line(0, 19) + _line(0, 3)
    assert recsync_stuck(log, passes=2) == (3, 2)


def test_old_line_format_without_adopted_is_ignored_not_crashed():
    # Pre-2026-08-07 passes logged no `adopted=`; they carry no verdict.
    old = "sync complete: uploaded=0 local_deleted=0\n"
    assert recsync_stuck(old + old, passes=2) is None
