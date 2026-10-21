"""
python manage.py check_payments

Explains why online payment (Paystack) is on or off, without printing the
secret key. Checks the .env file the server reads, duplicate or placeholder
lines, the key format, and (optionally) whether Paystack accepts the key.
"""

import json
import os
import re
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand

from api import payments


def mask(value):
    if not value:
        return "(empty)"
    return f"{value[:8]}…{value[-4:]} ({len(value)} characters)"


class Command(BaseCommand):
    help = "Check the Paystack payment setup and explain any problem."

    def add_arguments(self, parser):
        parser.add_argument("--offline", action="store_true", help="Don't contact Paystack to test the key.")

    def handle(self, *args, **opts):
        ok = True
        out = self.stdout
        good = lambda m: out.write(self.style.SUCCESS("  OK   ") + m)
        bad = lambda m: out.write(self.style.ERROR("  FIX  ") + m)
        info = lambda m: out.write("       " + m)

        out.write("\nPaystack setup check\n")

        env_file = getattr(settings, "ENV_FILE", None)
        if not getattr(settings, "DOTENV_AVAILABLE", True):
            ok = False
            bad("python-dotenv is not installed, so .env is never read. Run: pip install python-dotenv")
        if env_file is not None:
            if os.path.exists(env_file):
                good(f".env file found: {env_file}")
            else:
                ok = False
                bad(f"No .env file at {env_file}. Create it there (next to manage.py).")
                if os.path.exists(str(env_file) + ".txt"):
                    info("Found .env.txt instead: rename it to .env (Notepad added .txt).")

        # Look at the raw lines for the common mistakes.
        lines = []
        if env_file is not None and os.path.exists(env_file):
            with open(env_file, encoding="utf-8-sig", errors="replace") as fh:
                lines = [(n, l.rstrip("\n")) for n, l in enumerate(fh, 1)]
        key_lines = [(n, l) for n, l in lines if re.match(r"\s*(export\s+)?PAYSTACK_SECRET_KEY\s*=", l)]
        if len(key_lines) > 1:
            ok = False
            bad(f"PAYSTACK_SECRET_KEY appears {len(key_lines)} times in .env (lines "
                f"{', '.join(str(n) for n, _ in key_lines)}). The LAST one is used. Keep only one line.")
        elif not key_lines and lines:
            ok = False
            bad("There is no PAYSTACK_SECRET_KEY line in .env.")

        if os.environ.get("PAYSTACK_SECRET_KEY") is not None and key_lines:
            raw_last = key_lines[-1][1].split("=", 1)[1].strip().strip('"').strip("'")
            if raw_last and os.environ.get("PAYSTACK_SECRET_KEY") != raw_last:
                info("Note: a PAYSTACK_SECRET_KEY set in Windows/system environment variables overrides .env.")

        key = str(getattr(settings, "PAYSTACK_SECRET_KEY", "") or "").strip()
        info(f"Secret key the server sees: {mask(key)}")
        if not key:
            ok = False
            bad("The secret key is empty.")
        elif "PASTE" in key.upper():
            ok = False
            bad("The secret key is still the placeholder. Replace it with your Test Secret Key from "
                "Paystack > Settings > API Keys & Webhooks.")
        elif key.startswith("pk_"):
            ok = False
            bad("That is the PUBLIC key (pk_…). PAYSTACK_SECRET_KEY needs the SECRET key (sk_test_… or sk_live_…).")
        elif not key.startswith(("sk_test_", "sk_live_")):
            ok = False
            bad("The secret key must start with sk_test_ (test) or sk_live_ (live). Check for extra characters or quotes.")
        else:
            good(f"Key format looks right ({'TEST' if key.startswith('sk_test_') else 'LIVE'} mode).")

        site = getattr(settings, "PUBLIC_SITE_URL", "")
        if site:
            good(f"PUBLIC_SITE_URL = {site} (patients return to {site}/book)")
        else:
            ok = False
            bad("PUBLIC_SITE_URL is empty. Set it to the booking site, e.g. http://localhost:3000")

        if payments.paystack_secret_key() and not opts["offline"]:
            req = urllib.request.Request(
                f"{settings.PAYSTACK_API_URL}/transaction?perPage=1",
                headers={"Authorization": f"Bearer {payments.paystack_secret_key()}", "User-Agent": "IsaluBooking/1.0"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    json.loads(resp.read().decode() or "{}")
                good("Paystack accepted the key.")
            except urllib.error.HTTPError as exc:
                ok = False
                if exc.code == 401:
                    bad("Paystack REJECTED the key (invalid key). Copy it again from your Paystack dashboard. "
                        "Example keys from documentation or chat won't work.")
                else:
                    bad(f"Paystack answered with HTTP {exc.code}. Try again shortly.")
            except Exception as exc:  # network problems
                info(f"Couldn't reach Paystack to test the key ({exc.__class__.__name__}). Check the internet connection.")

        out.write("")
        if ok and payments.paystack_enabled():
            out.write(self.style.SUCCESS("Online payment is ON. Restart the server if it was running before you edited .env.\n"))
        else:
            out.write(self.style.ERROR("Online payment is OFF. Fix the items marked FIX, save .env, then restart the server.\n"))
