"""
python manage.py reconcile_payments

Runs the automatic payment check once: confirms bookings Paystack says were
paid, catches late payments on older attempts, reversed payments and payments
with no booking. The web server already runs it every PAYMENT_SWEEP_MINUTES;
use this from cron when that is switched off (PAYMENT_SWEEP_MINUTES=0).
"""

import json

from django.core.management.base import BaseCommand

from api.payment_ops import run_sweep


class Command(BaseCommand):
    help = "Check unconfirmed and recently paid online payments with Paystack."

    def handle(self, *args, **opts):
        result = run_sweep()
        self.stdout.write(json.dumps(result, indent=2, default=str))
