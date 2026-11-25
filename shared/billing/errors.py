"""The refusal shape, in one place.

Two consumers, two dialects, and getting this wrong has a specific bad
outcome. `outbound_dial` answers `{"ok": false, "reason": ...}` and the
Outbound UI reads `reason`; FastAPI's HTTPException produces `{"detail": ...}`
and the SPA folds that into a generic message. A refusal that only carries
`detail` therefore renders as "something went wrong", which nudges the
operator toward the Force-call button — i.e. toward retrying past the quota.

So every billing refusal carries BOTH, plus a machine `code` the SPA switches
on and an `upgrade` link. It must never carry `duplicate: true`: that is the
409 duplicate-dial shape, and `?force=1` bypasses it. Billing is not
force-bypassable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException

# Machine-readable refusal codes. The SPA maps these to copy and to whether a
# retry is even offered.
CODE_QUOTA_MINUTES = "quota_minutes"
CODE_CAP_AGENTS = "cap_agents"
CODE_CAP_CONCURRENCY = "cap_concurrency"
CODE_QUOTA_COPILOT = "quota_copilot"
# Distinct from CODE_QUOTA_COPILOT on purpose. Both refuse the same button, but
# they mean different things to the person reading the message and to whoever
# reads the logs afterwards: `quota_copilot` is "you have sent today's
# messages", `quota_copilot_tokens` is "your messages did today's WORK" — a
# tenant can hit the second on the fourth message of the day by driving long
# tool loops. Collapsing them into one code would have made an 8-message day
# report as "you have used today's 20 messages", which is simply false.
CODE_QUOTA_COPILOT_TOKENS = "quota_copilot_tokens"
CODE_PLAN_REQUIRED = "plan_required"
CODE_ACCOUNT_LOCKED = "account_locked"
CODE_UNATTRIBUTED = "unattributed"

# Codes for which offering a retry is actively harmful: nothing changes on
# retry, and the force-dial path must not present itself as a way through.
NON_RETRYABLE = frozenset({
    CODE_QUOTA_MINUTES, CODE_CAP_AGENTS, CODE_CAP_CONCURRENCY,
    CODE_QUOTA_COPILOT, CODE_QUOTA_COPILOT_TOKENS, CODE_PLAN_REQUIRED,
    CODE_ACCOUNT_LOCKED,
})


def refusal_body(reason: str, code: str, **extra) -> dict:
    """The one JSON body every refusal uses, in both dialects."""
    body = {
        "ok": False,
        "reason": reason,
        "detail": reason,
        "code": code,
        "upgrade": "/billing",
    }
    body.update(extra)
    body.pop("duplicate", None)  # never the duplicate-dial shape
    return body


class BillingRefusal(HTTPException):
    """402 Payment Required, carrying the full refusal body.

    Subclasses HTTPException on purpose: handlers that wrap their work in
    `except Exception -> 500` must re-raise HTTPException untouched, and the
    agent-creation path already has that guard. A bare ValueError would have
    surfaced as a 500 and the SPA's 402 handling would never fire.
    """

    def __init__(self, reason: str, code: str, **extra):
        super().__init__(status_code=402, detail=refusal_body(reason, code, **extra))
        self.code = code
        self.reason = reason


@dataclass
class Decision:
    """The result of an entitlement check.

    Returned rather than raised, because most call sites are not HTTP handlers
    — the inbound bridge, the autodialer sweep and the campaign loop all need
    to decide what to do next rather than unwind a stack.
    """

    allowed: bool
    code: str = ""
    reason: str = ""
    # Set when the check reserved capacity that must be released later.
    reservation: str = ""
    # 'ok' | 'error' | 'unattributed' — stamped on the usage_ledger row so
    # fail-open minutes stay identifiable after the fact.
    check: str = "ok"
    detail: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed

    def raise_for_http(self) -> None:
        if not self.allowed:
            raise BillingRefusal(self.reason, self.code, **self.detail)

    @classmethod
    def ok(cls, check: str = "ok", reservation: str = "", **detail) -> "Decision":
        # `reservation` is an explicit parameter, not part of **detail: as a
        # kwarg it landed in the detail dict, `.reservation` stayed empty, and
        # every release_reservation() call became a no-op — so admissions
        # accumulated until they aged out and a tenant's concurrency cap
        # slowly strangled them.
        return cls(allowed=True, check=check, reservation=reservation,
                   detail=detail)

    @classmethod
    def deny(cls, code: str, reason: str, check: str = "ok", **detail) -> "Decision":
        # `check` is explicit for the same reason `reservation` is on ok():
        # as a kwarg it vanished into detail, so an unattributed refusal was
        # stamped entitlement_check='ok' on the ledger row — losing exactly
        # the marker that makes such calls findable afterwards.
        return cls(allowed=False, code=code, reason=reason, check=check,
                   detail=detail)
