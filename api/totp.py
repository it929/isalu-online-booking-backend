"""
Time-based one-time codes (RFC 6238), as used by Google Authenticator,
Microsoft Authenticator, Authy and similar apps: 6 digits, a new code every
30 seconds, SHA-1. Standard library only.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP = 30
DIGITS = 6


def new_secret():
    """A random 160-bit secret, as base32 without padding (what the apps accept)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret):
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret, counter):
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def current_counter(now=None):
    return int((now if now is not None else time.time()) // STEP)


def verify(secret, code, last_counter=-1, window=1, now=None):
    """
    The counter the code matched (to store, so the same code can't be used
    twice), or None. Accepts the previous, current and next 30-second code
    to allow for clock drift; never a counter at or before `last_counter`.
    """
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    now_c = current_counter(now)
    for c in range(now_c - window, now_c + window + 1):
        if c > last_counter and hmac.compare_digest(code_at(secret, c), code):
            return c
    return None


def provisioning_uri(secret, account, issuer="Isalu Hospitals Tools"):
    label = urllib.parse.quote(f"{issuer}:{account}")
    query = urllib.parse.urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{query}"
