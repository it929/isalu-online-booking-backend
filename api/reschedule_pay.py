"""
Paid rescheduling.

Private Self-Pay patients who reschedule online pay a service charge of
RESCHEDULE_FEE_PERCENT (default 25%) of the clinic's fee for their visit type,
online with Paystack, before the booking moves. HMO and Company patients
reschedule free of charge.

1. quote()  - what the charge is for this booking (and doctor, if switching).
2. start()  - checks the new date exactly as a free reschedule would, records
              a ReschedulePayment and opens a Paystack checkout. If the clinic
              has no fee, the booking moves straight away (nothing to pay).
3. confirm_reschedule() - once Paystack confirms (browser return, webhook,
              automatic check or staff), the booking moves to the new date.
              If that date filled up meanwhile, a refund is opened instead.

Hospital staff editing a booking on the dashboard, and the hospital moving a
whole clinic, are not charged.
"""

import datetime
import logging
import secrets
import urllib.parse
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import payment_ledger as ledger
from . import payments
from .models import Booking, Doctor, PaymentAttempt, ReschedulePayment

logger = logging.getLogger(__name__)


class RescheduleError(Exception):
    """A problem the patient can act on (shown on the page)."""


def fee_percent():
    try:
        return max(0, min(100, int(getattr(settings, "RESCHEDULE_FEE_PERCENT", 25))))
    except (TypeError, ValueError):
        return 25


def _doctor(booking, doctor_id=None):
    key = str(doctor_id or booking.doctor_id or "").strip()
    return Doctor.objects.select_related("department").filter(doc_id__iexact=key).first() if key else None


def is_sponsored(booking):
    pt = str(getattr(booking, "payment_type", "") or "").strip().lower()
    return "hmo" in pt or "insurance" in pt or "company" in pt


def quote(booking, doctor_id=None):
    """{"base_fee", "percent", "amount"} in naira (amount rounded to the naira).
    HMO and Company bookings are not charged (amount 0)."""
    if is_sponsored(booking):
        return {"base_fee": Decimal("0.00"), "percent": 0, "amount": Decimal("0.00")}
    doctor = _doctor(booking, doctor_id)
    base = Decimal(payments.clinic_fee(doctor, booking.visit_type) if doctor else 0)
    pct = fee_percent()
    amount = (base * pct / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return {"base_fee": base.quantize(Decimal("0.01")), "percent": pct, "amount": amount.quantize(Decimal("0.01"))}


def _callback_url(request):
    base = getattr(settings, "PUBLIC_SITE_URL", "")
    if not base:
        from urllib.parse import urlparse
        origin = str(request.data.get("return_origin") or request.headers.get("Origin") or "").rstrip("/")
        allowed = set(getattr(settings, "CORS_ALLOWED_ORIGINS", []) or [])
        host = urlparse(origin).hostname or ""
        if origin and (origin in allowed or settings.DEBUG or host in ("localhost", "127.0.0.1")):
            base = origin
    if not base:
        raise RescheduleError("Online payment isn't fully set up yet. Please contact reception to reschedule.")
    return f"{base}/check-appointment?reschedule=return"


def start(request, booking, payload):
    """
    `payload` = the reschedule fields (date, time, doctor_id?, reschedule_reason, status).
    Returns {"applied": True, "booking": Booking} when there is nothing to pay,
    otherwise {"authorization_url", "reference", "amount", "percent"}.
    Raises RescheduleError, or rest_framework ValidationError for date problems.
    """
    from .serializers import BookingSerializer

    # Same checks a free reschedule runs (duty day, capacity, double booking, windows).
    serializer = BookingSerializer(booking, data=payload, partial=True, context={"request": request})
    serializer.is_valid(raise_exception=True)

    q = quote(booking, payload.get("doctor_id"))
    if q["amount"] <= 0:
        booking = serializer.save()
        Booking.objects.filter(ref_code=booking.ref_code).update(reminder_sent=False, reminder_sent_at=None)
        return {"applied": True, "booking": booking, **q}

    if not payments.paystack_enabled():
        raise RescheduleError("Online payment is switched off, so rescheduling online isn't available. Please contact reception.")

    callback = _callback_url(request)
    # An earlier unpaid reschedule for this booking is replaced by this one.
    ReschedulePayment.objects.filter(booking_ref=booking.ref_code, status=ReschedulePayment.STATUS_INITIALIZED).update(
        status=ReschedulePayment.STATUS_REPLACED)

    v = serializer.validated_data
    new_doctor = str(payload.get("doctor_id") or "").strip()
    reference = f"ISALU-RS-{booking.ref_code[-8:]}-{secrets.token_hex(3).upper()}"
    rp = ReschedulePayment.objects.create(
        reference=reference, booking_ref=booking.ref_code, old_date=booking.date, old_time=booking.time or "",
        new_date=str(v.get("date") or payload.get("date")), new_time=str(v.get("time", payload.get("time")) or ""),
        new_doctor_id=new_doctor if new_doctor and new_doctor.lower() != str(booking.doctor_id).lower() else "",
        reason=str(payload.get("reschedule_reason") or "")[:300],
        base_fee=q["base_fee"], percent=q["percent"], amount=q["amount"],
    )
    try:
        data = payments._paystack("POST", "/transaction/initialize", {
            "email": payments._patient_email(booking),
            "amount": payments.to_kobo(rp.amount),
            "currency": "NGN",
            "reference": reference,
            "callback_url": callback,
            "metadata": {
                "ref_code": booking.ref_code,
                "purpose": "reschedule",
                "patient_name": booking.patient_name,
                "new_date": rp.new_date,
                "cancel_action": callback + f"&cancelled={booking.ref_code}",
                "custom_fields": [
                    {"display_name": "Booking reference", "variable_name": "ref_code", "value": booking.ref_code},
                    {"display_name": "Purpose", "variable_name": "purpose", "value": f"Reschedule to {rp.new_date}"},
                ],
            },
        })
    except payments.PaymentError as exc:
        rp.status = ReschedulePayment.STATUS_REPLACED
        rp.note = str(exc)[:500]
        rp.save(update_fields=["status", "note"])
        raise RescheduleError(str(exc))
    PaymentAttempt.objects.get_or_create(reference=reference, defaults={
        "booking_ref": booking.ref_code, "amount_kobo": payments.to_kobo(rp.amount), "currency": "NGN"})
    return {"authorization_url": data.get("authorization_url"), "reference": reference,
            "amount": rp.amount, "percent": rp.percent, "base_fee": rp.base_fee}


def is_reschedule_reference(reference):
    return bool(reference) and ReschedulePayment.objects.filter(reference=reference).exists()


def confirm_reschedule(reference, transaction_data=None, source="verify", actor="", ip=""):
    """
    Apply a paid reschedule. Safe to call from several places at once.
    Returns (booking, outcome): "rescheduled", "already_rescheduled", "pending",
    "failed", "underpaid", "not_applied", "not_found".
    """
    rp = ReschedulePayment.objects.filter(reference=reference).first()
    if rp is None:
        return None, "not_found"
    tx = transaction_data
    if tx is None:
        tx = payments._paystack("GET", f"/transaction/verify/{urllib.parse.quote(reference, safe='')}")
    payments._note_attempt(reference, rp.booking_ref, tx)
    status = str(tx.get("status") or "").lower()
    booking = Booking.objects.filter(ref_code=rp.booking_ref).first()
    if status != "success":
        if rp.status == ReschedulePayment.STATUS_APPLIED:
            return booking, "already_rescheduled"
        return booking, ("failed" if status in ("failed", "reversed") else "pending")

    paid_kobo = int(tx.get("amount") or 0)
    paid = (Decimal(paid_kobo) / 100).quantize(Decimal("0.01"))
    from .serializers import BookingSerializer

    with transaction.atomic():
        rp = ReschedulePayment.objects.select_for_update().get(pk=rp.pk)
        if rp.status == ReschedulePayment.STATUS_APPLIED:
            return booking, "already_rescheduled"
        if rp.status in (ReschedulePayment.STATUS_NOT_APPLIED, ReschedulePayment.STATUS_FAILED):
            return booking, ("not_applied" if rp.status == ReschedulePayment.STATUS_NOT_APPLIED else "underpaid")
        booking = Booking.objects.select_for_update().filter(ref_code=rp.booking_ref).first()
        rp.paid_at = timezone.now()

        if str(tx.get("currency") or "NGN").upper() != "NGN" or paid_kobo < payments.to_kobo(rp.amount):
            rp.status = ReschedulePayment.STATUS_FAILED
            rp.note = f"Paid {paid}, the reschedule charge is {rp.amount}."
            rp.save()
            if ledger.record("underpaid", reference=reference, booking=booking, amount=paid, source=source, actor=actor,
                             ip=ip, once=True, note=f"Reschedule charge short: {rp.note}"):
                ledger.open_refund(reference=reference, booking=booking, amount=paid, reason="underpaid",
                                   note="Paid less than the reschedule charge.")
            return booking, "underpaid"

        problem = ""
        if booking is None:
            problem = "The booking no longer exists."
        elif not booking.counts_toward_capacity or str(booking.status) in ("Checked In", "Completed"):
            problem = "The appointment can no longer be moved (checked in, completed or cancelled)."
        else:
            data = {"date": rp.new_date, "time": rp.new_time, "status": "Confirmed"}
            if rp.new_doctor_id:
                data["doctor_id"] = rp.new_doctor_id
            serializer = BookingSerializer(booking, data=data, partial=True, context={})
            if serializer.is_valid():
                booking = serializer.save()
                Booking.objects.filter(ref_code=booking.ref_code).update(reminder_sent=False, reminder_sent_at=None)
            else:
                from .views import flatten_error_detail
                problem = str(flatten_error_detail(serializer.errors).get("error") or "The new date is no longer available.")

        if problem:
            rp.status = ReschedulePayment.STATUS_NOT_APPLIED
            rp.note = problem[:500]
            rp.save()
            ledger.record("reschedule_not_applied", reference=reference, booking=booking, amount=paid, source=source,
                          actor=actor, ip=ip, once=True, note=f"Paid, but couldn't move to {rp.new_date}: {problem}")
            ledger.open_refund(reference=reference, booking=booking, amount=paid, reason="reschedule_failed",
                               note=f"Reschedule to {rp.new_date} couldn't be applied: {problem}"[:500])
            return booking, "not_applied"

        rp.status = ReschedulePayment.STATUS_APPLIED
        rp.applied_at = timezone.now()
        rp.save()
        ledger.record("reschedule_paid", reference=reference, booking=booking, amount=paid, source=source, actor=actor,
                      ip=ip, note=f"Moved from {rp.old_date} to {rp.new_date} ({rp.percent}% of {rp.base_fee}).",
                      data={"old_date": rp.old_date, "new_date": rp.new_date, "new_doctor_id": rp.new_doctor_id})
        ledger.notify_patient(booking.ref_code, "rescheduled", amount=paid)

    try:
        from .views import broadcast_booking_update
        broadcast_booking_update(booking, event_type="BOOKING_UPDATED",
                                 message=f"{booking.patient_name} ({booking.ref_code}) rescheduled to {booking.date} (fee paid).")
    except Exception:
        logger.exception("broadcast failed for %s", booking.ref_code)
    return booking, "rescheduled"


def reschedule_payload(rp):
    return {
        "reference": rp.reference, "booking_ref": rp.booking_ref, "old_date": rp.old_date, "old_time": rp.old_time,
        "new_date": rp.new_date, "new_time": rp.new_time, "new_doctor_id": rp.new_doctor_id,
        "base_fee": float(rp.base_fee), "percent": rp.percent, "amount": float(rp.amount), "status": rp.status,
        "note": rp.note, "created_at": timezone.localtime(rp.created_at).isoformat() if rp.created_at else None,
        "applied_at": timezone.localtime(rp.applied_at).isoformat() if rp.applied_at else None,
    }
