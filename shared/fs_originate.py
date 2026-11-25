"""The FreeSWITCH originate string — ONE definition, shared by every caller.

Extracted from dashboard/server.py, which was the only place that knew how to
build one. It stops being the only place the moment a second surface can dial:
the admin plane's "test this number" needs the identical channel variables, and
a second hand-written copy is how they drift.

What must not drift, and why (RUNBOOK §33)
------------------------------------------
`lescall_dest` carries the DIALLED NUMBER on the channel. It is already in the
dial string, but FreeSWITCH sets the resulting channel's
Caller-Destination-Number to the DIALPLAN EXTENSION (`outbound_ai_connect`),
not to the number — so the telephony bridge has no first-hand way to know who
this leg is calling. It used to fall back to "the most recently logged dial",
which during a burst is somebody else: 44 calls greeted a real person by a
stranger's name before 2026-08-29. A channel variable cannot race — it belongs
to this leg and no other.

Omitting it from a second originate builder would reintroduce exactly that bug
on the new surface only, which is the kind of regression that takes months to
be believed. Hence this module, and hence `test_originate_carries_the_dialled_number`.

The destination is normalized HERE, not at the call sites
------------------------------------------------------------------
A bare national number ("7359800826") is not dialable on a carrier trunk: the
gateway answers the INVITE with SIP 404 and FreeSWITCH hangs up
UNALLOCATED_NUMBER, which the dashboard reports to the operator as the flat
word "declined" — indistinguishable from a human pressing reject. One tenant
burned four dials on that in two minutes on 2026-09-09 before anyone looked at
a SIP trace.

Every write path was supposed to store +E.164 (`shared.phone.to_e164`), and
most do. But a phone book is not a closed system — hand-typed rows, older
imports, a CRM push through some future surface — and there are seven originate
origins. Normalizing at each of them is seven chances to forget; normalizing at
the one place they all funnel through cannot be forgotten. This module is that
place, so this is where it belongs.
"""

from __future__ import annotations

import os

# The dialplan extension every AI outbound leg is bridged into. Not an env var
# in the original either — it is the name of a real extension in the shipped
# dialplan, so changing it means editing FreeSWITCH config too.
OUTBOUND_DP_EXT = "outbound_ai_connect"
OUTBOUND_DP_CTX = "default"

# Founding-account fallbacks, used only when a caller supplies neither. Every
# tenant-scoped path passes its own number and gateway explicitly.
DEFAULT_CALLER_ID = os.environ.get("OUTBOUND_CALLER_ID", "918065953160")
DEFAULT_GATEWAY = os.environ.get("OUTBOUND_GATEWAY_NAME", "plivo-outbound")

ORIGINATE_TIMEOUT_S = 45


def dialable_destination(phone: str) -> str:
    """The digits to hand a carrier gateway for `phone`.

    `to_e164` supplies the country code an Indian national number was typed
    without; the leading "+" is dropped because that is the form the trunks
    already take on every call that has ever connected here.

    A number `to_e164` cannot place (under 7 digits, or nothing but
    punctuation) is passed through as its own digits rather than blanked. This
    function exists to make an undialable string dialable, and it must never be
    the reason a string that WAS dialable stops being one — short internal
    extensions are the case that would break.
    """
    from shared.phone import digits_only, to_e164

    raw = digits_only(phone)[:20]
    e164 = to_e164(raw)
    return (digits_only(e164) or raw)[:20]


def originate_cmd(phone: str, agent_slug: str = "", campaign_id: str = "",
                  caller_id: str = "", gateway_name: str = "") -> str:
    """Build the `originate` for one outbound AI leg.

    Egress is ALWAYS through a named gateway (`sofia/gateway/<name>/…`) so the
    call uses that trunk's own credentials and caller-id authorisation — never
    a bare domain that could resolve to the shared profile and send a caller ID
    the carrier has not authorised for that account.
    """
    caller_id = caller_id or DEFAULT_CALLER_ID
    gateway_name = gateway_name or DEFAULT_GATEWAY

    dest_digits = dialable_destination(phone)
    chan_vars = [
        f"origination_caller_id_number={caller_id}",
        f"originate_timeout={ORIGINATE_TIMEOUT_S}",
        "ignore_early_media=true",
    ]
    if dest_digits:
        chan_vars.append(f"lescall_dest={dest_digits}")
    if agent_slug:
        chan_vars.append(f"lescall_agent_slug={agent_slug}")
    if campaign_id:
        chan_vars.append(f"lescall_campaign_id={campaign_id}")
    return (
        "originate {" + ",".join(chan_vars) + "}"
        f"sofia/gateway/{gateway_name}/{dest_digits} "
        f"{OUTBOUND_DP_EXT} XML {OUTBOUND_DP_CTX}"
    )
