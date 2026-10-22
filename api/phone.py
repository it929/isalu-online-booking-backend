"""
Nigerian mobile number check for online bookings.

Accepted (spaces, dashes, dots and brackets are ignored):
  0803 123 4567     08031234567     0803-123-4567     (0803) 123-4567
  803 123 4567      (10 digits, leading 0 left out)

Numbers written with the country code (+234..., 234..., 00234...) are not
accepted: patients enter the local number starting with 0.

The number must be a Nigerian mobile: 070x, 071x, 080x, 081x, 090x or 091x
followed by the rest of the 11-digit number. Landlines aren't accepted because
appointment reminders are sent by SMS.
"""

import re

_MOBILE = re.compile(r"^0[789][01]\d{8}$")
INVALID_MESSAGE = (
    "Enter a valid Nigerian mobile number, e.g. 0803 123 4567. "
    "It must start with 070, 071, 080, 081, 090 or 091 and have 11 digits."
)
COUNTRY_CODE_MESSAGE = (
    "Enter your number starting with 0 (e.g. 0803 123 4567), without +234 or 234."
)


def _has_country_code(text):
    digits = re.sub(r"\D", "", text)
    return text.lstrip().startswith("+") or digits.startswith("234") or digits.startswith("00234")


def to_local_mobile(raw):
    """'0803 123 4567' -> '08031234567', or None if it isn't an accepted Nigerian mobile number."""
    text = str(raw or "").strip()
    if not text or re.search(r"[^\d\s().\-]", text) or _has_country_code(text):
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10 and not digits.startswith("0"):
        digits = "0" + digits
    return digits if _MOBILE.match(digits) else None


def nigerian_mobile_problem(raw):
    """"" when the number is a valid Nigerian mobile, otherwise a message for the patient."""
    if not str(raw or "").strip():
        return "Enter your phone number."
    if _has_country_code(str(raw)):
        return COUNTRY_CODE_MESSAGE
    return "" if to_local_mobile(raw) else INVALID_MESSAGE
