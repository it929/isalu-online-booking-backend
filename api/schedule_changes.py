"""
One-off clinic changes: cancel a doctor's clinic on a specific date, or move
that single occurrence to another date. The regular schedule is untouched.

Flow
----
1. apply_schedule_exception() validates the request, then in one database
   transaction records a ScheduleException and updates every affected
   booking (cancelled, or moved to the new date with its reminder reset so
   the automatic 3-hour reminder fires for the new date).
2. After the transaction commits, patients are emailed and texted in a
   background thread; per-patient results are stored on the exception so
   staff can see who was reached and retry failures.
"""

import logging
import random
import threading
import time

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import Booking, Doctor, ScheduleException
from .scheduling import (
    capacity_bookings_qs,
    clear_exception_cache,
    parse_date,
    resolve_doctor_day,
)

logger = logging.getLogger(__name__)

# Bookings that have already been seen are never cancelled or moved.
FINISHED_STATUSES = ("checked in", "completed")


class ScheduleChangeError(ValueError):
    pass


def _generate_id():
    return f"exc-{int(time.time() * 1000)}-{random.randint(100, 999)}"


def affected_bookings_qs(doctor, date_iso):
    qs = capacity_bookings_qs().filter(doctor_id=doctor.doc_id, date=date_iso)
    for status in FINISHED_STATUSES:
        qs = qs.exclude(status__iexact=status)
    return qs.order_by("time", "created_at")


def preview_schedule_change(doctor, date_val):
    """What would be affected if the clinic on date_val were changed."""
    day = parse_date(date_val)
    if not day:
        raise ScheduleChangeError("Invalid date. Use YYYY-MM-DD.")
    regular = resolve_doctor_day(doctor, day, ignore_exceptions=True)
    current = resolve_doctor_day(doctor, day)
    bookings = list(affected_bookings_qs(doctor, day.isoformat()))
    return {
        "doctor_id": doctor.doc_id,
        "date": day.isoformat(),
        "on_duty": bool(regular["on_duty"]),
        "already_changed": current["exception"] is not None,
        "note": current["note"],
        "shift": regular["shift"],
        "capacity": regular["capacity"] if regular["on_duty"] else 0,
        "affected_count": len(bookings),
        "with_email": sum(1 for b in bookings if "@" in (b.patient_email or "")),
        "with_phone": sum(1 for b in bookings if len((b.patient_phone or "").strip()) >= 7),
        "patients": [
            {"ref_code": b.ref_code, "patient_name": b.patient_name, "time": b.time}
            for b in bookings
        ],
    }


def apply_schedule_exception(
    doctor_id,
    original_date,
    action,
    new_date=None,
    shift_times=None,
    reason="",
    created_by="",
):
    if action not in (ScheduleException.ACTION_CANCEL, ScheduleException.ACTION_RESCHEDULE):
        raise ScheduleChangeError("Action must be 'cancel' or 'reschedule'.")

    today = timezone.localdate()
    original = parse_date(original_date)
    if not original:
        raise ScheduleChangeError("Choose a valid clinic date (YYYY-MM-DD).")
    if original < today:
        raise ScheduleChangeError("You can only change today's or future clinic dates.")

    reason = str(reason or "").strip()[:500]
    if isinstance(shift_times, str):
        shift_times = [shift_times]
    shift_times = [str(t).strip() for t in (shift_times or []) if str(t).strip()]

    with transaction.atomic():
        doctor = (
            Doctor.objects.select_for_update()
            .filter(doc_id=str(doctor_id or "").strip())
            .first()
        )
        if not doctor:
            raise ScheduleChangeError("Doctor not found.")
        display = doctor.full_name or doctor.name

        if ScheduleException.objects.filter(doctor=doctor, original_date=original.isoformat()).exists():
            raise ScheduleChangeError(
                f"The {original.strftime('%a %d %b %Y')} clinic for {display} has already been changed. "
                "Undo that change first."
            )
        if ScheduleException.objects.filter(doctor=doctor, new_date=original.isoformat()).exists():
            raise ScheduleChangeError(
                f"{original.isoformat()} is itself a moved clinic. Undo that move instead of changing it again."
            )

        regular = resolve_doctor_day(doctor, original, ignore_exceptions=True)
        if not regular["on_duty"]:
            raise ScheduleChangeError(
                f"{display} has no clinic on {original.strftime('%A %d %b %Y')}, so there is nothing to change."
            )

        target = None
        if action == ScheduleException.ACTION_RESCHEDULE:
            target = parse_date(new_date)
            if not target:
                raise ScheduleChangeError("Choose the new date for this clinic.")
            if target < today:
                raise ScheduleChangeError("The new date cannot be in the past.")
            if target == original:
                raise ScheduleChangeError("The new date must be different from the original date.")
            clear_exception_cache(doctor)
            target_state = resolve_doctor_day(doctor, target)
            if target_state["on_duty"]:
                raise ScheduleChangeError(
                    f"{display} already has a clinic on {target.strftime('%A %d %b %Y')}. "
                    "Choose a date without a clinic."
                )
            if target_state["exception"] is not None:
                raise ScheduleChangeError(
                    f"{target.isoformat()} is already involved in another schedule change for {display}."
                )
            if not shift_times:
                shift_times = list(regular["config"].get("shiftTimes") or []) or (
                    [regular["shift"]] if regular["shift"] else []
                )

        bookings = list(affected_bookings_qs(doctor, original.isoformat()))
        refs = [b.ref_code for b in bookings]

        exception = ScheduleException.objects.create(
            exception_id=_generate_id(),
            doctor=doctor,
            original_date=original.isoformat(),
            action=action,
            new_date=target.isoformat() if target else "",
            shift_times=shift_times if target else [],
            capacity=max(regular["capacity"], len(refs)) if target else 0,
            reason=reason,
            affected_refs=refs,
            affected_count=len(refs),
            notification_status="pending" if refs else "none",
            created_by=str(created_by or "")[:200],
        )

        if action == ScheduleException.ACTION_CANCEL:
            note = f"Clinic cancelled for {original.isoformat()}" + (f": {reason}" if reason else "")
            for b in bookings:
                b.status = "Cancelled"
                b.delete_reason = note[:1000]
                b.save(update_fields=["status", "delete_reason"])
        else:
            new_time = shift_times[0] if shift_times else None
            for b in bookings:
                b.date = target.isoformat()
                if new_time:
                    b.time = new_time
                # Re-arm the automatic reminder for the new date.
                b.reminder_sent = False
                b.reminder_sent_at = None
                b.save(update_fields=["date", "time", "reminder_sent", "reminder_sent_at"])

        if refs:
            transaction.on_commit(lambda: start_notifications(exception.exception_id))

    return exception


def _send_notifications(exception_id, only_refs=None):
    close_old_connections()
    try:
        from .notification_service import send_schedule_change_notice

        exc = ScheduleException.objects.filter(exception_id=exception_id).first()
        if not exc:
            return
        refs = list(only_refs or exc.affected_refs or [])
        exc.notification_status = "sending"
        exc.save(update_fields=["notification_status"])

        previous = {
            entry.get("ref_code"): entry
            for entry in (exc.notification_log or [])
            if entry.get("ref_code") not in refs
        }
        results = []
        for booking in Booking.objects.filter(ref_code__in=refs):
            try:
                result = send_schedule_change_notice(
                    booking, exc.action, exc.original_date, exc.new_date, exc.reason
                )
            except Exception as error:  # never let one patient stop the rest
                logger.exception("Schedule-change notice failed for %s", booking.ref_code)
                result = {
                    "ref_code": booking.ref_code,
                    "patient_name": booking.patient_name,
                    "delivered": False,
                    "email_sent": False,
                    "sms_sent": False,
                    "error": str(error),
                }
            result["sent_at"] = timezone.now().isoformat()
            results.append(result)

        merged = list(previous.values()) + results
        delivered = sum(1 for r in merged if r.get("delivered"))
        failed = len(merged) - delivered
        exc.notification_log = merged
        exc.notified_count = delivered
        exc.notification_failed_count = failed
        exc.notification_status = (
            "none" if not merged else "sent" if failed == 0 else "failed" if delivered == 0 else "partial"
        )
        exc.save(update_fields=[
            "notification_log", "notified_count", "notification_failed_count", "notification_status",
        ])
    finally:
        close_old_connections()


def start_notifications(exception_id, only_refs=None):
    """
    Send in the background so the admin's request returns immediately.
    Set SCHEDULE_CHANGE_NOTIFY_SYNC = True (e.g. in tests) to send inline.
    """
    if getattr(settings, "SCHEDULE_CHANGE_NOTIFY_SYNC", False):
        _send_notifications(exception_id, only_refs)
        return
    threading.Thread(
        target=_send_notifications,
        args=(exception_id, only_refs),
        daemon=True,
        name=f"schedule-change-{exception_id}",
    ).start()


def retry_failed_notifications(exception):
    failed = [
        entry.get("ref_code")
        for entry in (exception.notification_log or [])
        if not entry.get("delivered")
    ]
    if failed:
        start_notifications(exception.exception_id, only_refs=failed)
    return len(failed)


def undo_schedule_exception(exception):
    """
    Only changes that affected no bookings can be undone automatically;
    otherwise patients would get contradictory messages.
    """
    if exception.affected_count:
        raise ScheduleChangeError(
            f"This change already affected {exception.affected_count} appointment(s) and patients were notified, "
            "so it cannot be undone automatically. Contact the affected patients directly."
        )
    exception.delete()
