"""Phone-number normalization shared by call routing and FreeSWITCH config generation.

Carriers deliver the same DID inconsistently — E.164 (+918065953160), national
(08065953160), or bare (8065953160). Everything that compares numbers must agree
on one canonical form: the trailing SIGNIFICANT_DIGITS digits (10 covers Indian
national numbers). Both the DB routing lookup (conversation_call_bridge) and the
generated dialplan regex (shared.freeswitch_config) derive from here so they can
never drift apart.
"""

import re

SIGNIFICANT_DIGITS = 10

# A phone number that arrived as a spreadsheet NUMBER rather than text.
# "9128692865.0" is what a float cell prints as; "9.128692865E9" is what Excel
# shows for it. Stripping "everything but digits" from either mangles the
# number — the first gains a trailing 0, the second loses eight digits — and
# a 20-row hiring list uploaded on 2026-09-25 reached the carrier as
# 91286928650, 83097276130, … : twenty UNALLOCATED_NUMBER rejections shown as
# "declined" (RUNBOOK §58). A phone number never legitimately carries a
# decimal point, so a fraction after seven or more digits is formatting noise.
_SCI_NOTATION_RE = re.compile(r"^\s*\+?(\d+(?:\.\d+)?)[eE]\+?(\d+)\s*$")
_FLOAT_FRACTION_RE = re.compile(r"(?<=\d{7})\.\d+(?=\D*$)")


def numeric_text(number: str | None) -> str:
    """`number` with spreadsheet float / scientific-notation artefacts undone.

    "9128692865.0"  -> "9128692865"
    "9.128692865E9" -> "9128692865"
    "+91 80659-53160", "079-23251306" -> unchanged
    """
    text = str(number or "").strip()
    m = _SCI_NOTATION_RE.match(text)
    if m:
        try:
            return str(int(float(m.group(0).strip().lstrip("+"))))
        except (ValueError, OverflowError):
            return text
    return _FLOAT_FRACTION_RE.sub("", text)


def digits_only(number: str | None) -> str:
    """Strip a phone number down to its digits ("+91 80659-53160" -> "918065953160")."""
    return "".join(ch for ch in numeric_text(number) if ch.isdigit())


def significant_digits(number: str | None, count: int = SIGNIFICANT_DIGITS) -> str:
    """Canonical routing key: the last `count` digits of the number.

    Returns fewer digits if the number is shorter (e.g. test extensions), or ""
    for empty/non-numeric input.
    """
    return digits_only(number)[-count:]


def dialplan_expression(number: str | None) -> str:
    """FreeSWITCH destination_number regex tolerant of country-code/prefix noise.

    Matches the significant digits with any leading +/country-code/zero prefix:
    ^(\\+?\\d*?)?8065953160$ matches +918065953160, 08065953160, 8065953160.
    """
    sig = significant_digits(number)
    if not sig:
        raise ValueError(f"no digits in phone number: {number!r}")
    return rf"^(\+?\d*?)?{sig}$"


def to_e164(number: str | None) -> str:
    """A dialable +E.164 string, assuming India for national-format numbers.

    The assumption is deliberate and load-bearing: this platform's numbers are
    Indian, and a 10-digit cell number typed without a country code is the most
    common thing a customer's CRM will push at us. Anything already carrying a
    country code (12 digits starting 91, or any 7-15 digit international
    number) is passed through untouched. Returns "" when nothing dialable can
    be recovered — callers must treat that as a rejected row, never as a blank
    phone number.
    """
    digits = digits_only(number)
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if 7 <= len(digits) <= 15:
        return "+" + digits
    return ""
