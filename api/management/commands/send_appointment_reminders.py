"""
Django Management Command: send_appointment_reminders
Sends Email and SMS reminders to patients scheduled for clinic appointments 1 day ahead.

Usage:
    python manage.py send_appointment_reminders
    python manage.py send_appointment_reminders --days-ahead 1
    python manage.py send_appointment_reminders --date 2026-09-06
    python manage.py send_appointment_reminders --force
"""

from django.core.management.base import BaseCommand
from api.notification_service import process_appointment_reminders


class Command(BaseCommand):
    help = "Sends Email and SMS reminders to patients scheduled for clinic appointments (1 day ahead or 3 hours prior)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            help="Target appointment date in YYYY-MM-DD format (e.g., 2026-09-06). Defaults to tomorrow.",
        )
        parser.add_argument(
            "--days-ahead",
            type=int,
            default=1,
            help="Number of days in advance to send reminders (default: 1 day ahead).",
        )
        parser.add_argument(
            "--hours-ahead",
            type=int,
            default=0,
            help="Send reminders for appointments starting within N hours (e.g., --hours-ahead 3).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force resend reminders even if already marked as sent.",
        )

    def handle(self, *args, **options):
        target_date = options.get("date")
        days_ahead = options.get("days_ahead", 1)
        hours_ahead = options.get("hours_ahead", 0)
        force = options.get("force", False)

        from api.notification_service import process_appointment_reminders, process_3hour_appointment_reminders

        if hours_ahead > 0:
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"[REMINDERS] Running {hours_ahead}-Hour Prior Appointment Reminders Service (Force: {force})..."
                )
            )
            summary = process_3hour_appointment_reminders(hours_ahead=hours_ahead, force=force)
        else:
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"[REMINDERS] Running Daily Appointment Reminders Service (Target Date: {target_date or f'{days_ahead} day(s) ahead'}, Force: {force})..."
                )
            )
            summary = process_appointment_reminders(
                target_date=target_date,
                days_ahead=days_ahead,
                force=force
            )

        total = summary["total_eligible"]
        processed = summary["processed_count"]
        failed = summary["failed_count"]
        t_date = summary.get("target_date") or f"appointments in upcoming {summary.get('hours_ahead', 3)} hours"

        if total == 0:
            self.stdout.write(
                self.style.WARNING(f"[INFO] No unsent eligible appointments found for {t_date}.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[SUCCESS] Successfully processed {processed}/{total} appointment reminders for {t_date} (Failed: {failed})."
                )
            )

        for rem in summary.get("reminders", []):
            if rem.get("success"):
                ref = rem.get("ref_code")
                pname = rem.get("patient_name")
                email_st = "Email OK" if rem.get("email_sent") else "Email NO"
                sms_st = "SMS OK" if rem.get("sms_sent") else "SMS NO"
                self.stdout.write(f"  * [{ref}] {pname} -> {email_st} | {sms_st}")
            else:
                self.stdout.write(
                    self.style.ERROR(f"  * [{rem.get('ref_code')}] Error: {rem.get('error') or rem.get('message')}")
                )
