"""
Set up Google Authenticator (or any authenticator app) for a superuser on the
hidden bookings tools page.

    python manage.py setup_tools_2fa <username>          set up / replace
    python manage.py setup_tools_2fa <username> --remove switch off for that user

It prints a setup key (and a QR code, if the `qrcode` package is installed).
Add it in the app, then type the 6-digit code the app shows to confirm.
Nothing is saved until a correct code is entered, so a typo can't lock you out.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api import totp
from api.models import AppSetting
from api.superuser_tools import TWOFA_KEY


class Command(BaseCommand):
    help = "Set up the authenticator app (two-step sign-in) for the bookings tools page."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--remove", action="store_true", help="Remove the authenticator for this user.")

    def handle(self, *args, **options):
        user = User.objects.filter(username=options["username"]).first()
        if not user or not user.is_superuser or not user.is_active:
            raise CommandError("No active superuser with that username.")
        key = TWOFA_KEY.format(user.pk)
        if options["remove"]:
            AppSetting.objects.filter(key=key).delete()
            self.stdout.write(self.style.WARNING(f"Authenticator removed for {user.username}. They can't use the tools page until it is set up again."))
            return

        secret = totp.new_secret()
        uri = totp.provisioning_uri(secret, user.username)
        grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
        self.stdout.write("")
        self.stdout.write("1. Open Google Authenticator and tap + > Enter a setup key.")
        self.stdout.write(f"   Account: Isalu Tools ({user.username})")
        self.stdout.write(f"   Key:     {grouped}")
        self.stdout.write("   Type of key: Time based")
        try:
            import qrcode  # optional
            qr = qrcode.QRCode(border=1)
            qr.add_data(uri)
            self.stdout.write("   ...or scan this QR code:")
            qr.print_ascii(out=self.stdout, invert=True)
        except ImportError:
            self.stdout.write("   (pip install qrcode to show a QR code here instead)")
        self.stdout.write("")
        for attempt in range(3):
            code = input("2. Type the 6-digit code the app now shows: ").strip()
            counter = totp.verify(secret, code)
            if counter is not None:
                AppSetting.objects.update_or_create(key=key, defaults={"value": {
                    "secret": secret, "confirmed_at": timezone.now().isoformat(), "last_counter": counter}})
                self.stdout.write(self.style.SUCCESS(f"Authenticator set up for {user.username}. Any older authenticator entry for this page no longer works."))
                return
            self.stdout.write(self.style.ERROR("That code doesn't match. Check the phone's time is set automatically and try again."))
        raise CommandError("Not set up: no matching code after 3 tries. Nothing was changed.")
