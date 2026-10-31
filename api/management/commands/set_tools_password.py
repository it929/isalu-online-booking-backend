"""
Set the password for the hidden superuser bookings tools page.

    python manage.py set_tools_password

The password is asked twice (not shown on screen) and stored hashed in the
database, never in .env or the code.
"""

import getpass

from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.models import AppSetting
from api.superuser_tools import PASSWORD_KEY


class Command(BaseCommand):
    help = "Set the password required by the superuser bookings tools page."

    def handle(self, *args, **options):
        first = getpass.getpass("New tools password (at least 12 characters): ")
        if len(first) < 12:
            raise CommandError("Use at least 12 characters.")
        try:
            validate_password(first)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages))
        if getpass.getpass("Repeat it: ") != first:
            raise CommandError("The two passwords don't match. Nothing was changed.")
        AppSetting.objects.update_or_create(
            key=PASSWORD_KEY, defaults={"value": {"hash": make_password(first), "set_at": timezone.now().isoformat()}})
        self.stdout.write(self.style.SUCCESS("Tools password saved."))
