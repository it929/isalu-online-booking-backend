"""
Online payment for private self-pay bookings (Paystack), and the HMO
minimum-notice rule.

Flow
----
1. The patient picks a doctor, date and time and clicks "Pay".
   `start_payment()` saves the booking as "Awaiting Payment" (this holds the
   slot for PAYMENT_HOLD_MINUTES) and opens a Paystack transaction for the
   clinic's consultation fee. The amount always comes from the clinic record
   on the server, never from the browser.
2. The patient pays on Paystack's checkout page and is sent back to
   <PUBLIC_SITE_URL>/book?reference=...
3. The booking page calls the verify endpoint; `confirm_payment()` asks
   Paystack whether the transaction succeeded and, if so, confirms the booking
   and marks it "Cleared". The Paystack webhook does the same, so a patient
   who closes the tab still gets a confirmed booking.
4. Holds that are not paid in time become "Payment Expired" and stop
   counting toward the doctor's capacity.
"""

import datetime
import hashlib
import hmac
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Booking, Doctor
from .scheduling import (
    AWAITING_PAYMENT,
    PAYMENT_EXPIRED,
    count_doctor_bookings,
    parse_date,
    parse_time_of_day,
    resolve_doctor_day,
)

logger = logging.getLogger(__name__)

ONLINE_METHOD = "Paystack (online)"
PAYMENT_STATUS_AWAITING = "Awaiting Online Payment"
PAYMENT_STATUS_PAID = "Cleared"
PAYMENT_STATUS_REFUND = "Refund Due"


class PaymentError(Exception):
    """A problem the patient can act on; the message is shown to them."""


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

def paystack_secret_key():
    """The configured secret key, ignoring the placeholder from .env.example."""
    key = str(getattr(settings, "PAYSTACK_SECRET_KEY", "") or "").strip()
    if not key or "PASTE" in key.upper() or not key.startswith(("sk_test_", "sk_live_")):
        return ""
    return key


def paystack_enabled():
    return bool(paystack_secret_key())


def hold_minutes():
    return max(5, int(getattr(settings, "PAYMENT_HOLD_MINUTES", 30)))


def hmo_notice_hours():
    return max(0, int(getattr(settings, "HMO_MIN_NOTICE_HOURS", 48)))


def is_hmo(payment_type):
    return "hmo" in str(payment_type or "").lower()


def is_company(payment_type):
    return "company" in str(payment_type or "").lower()


def is_sponsored(payment_type):
    """HMO and Company patients: covered by their sponsor, approved at the HMO desk."""
    return is_hmo(payment_type) or is_company(payment_type)


def clinic_fee(doctor):
    """The doctor's clinic consultation fee in Naira (Decimal), or 0."""
    if doctor is None or not doctor.department_id:
        return Decimal("0")
    fee = doctor.department.consultation_fee or Decimal("0")
    return Decimal(fee).quantize(Decimal("0.01"))


def payment_mandatory():
    return bool(getattr(settings, "PRIVATE_BOOKINGS_REQUIRE_PAYMENT", True))


def requires_online_payment(doctor, payment_type):
    """
    Private self-pay bookings made online must be paid before a ticket is issued
    (unless PRIVATE_BOOKINGS_REQUIRE_PAYMENT is switched off, in which case only
    clinics with a fee are paid online and the rest pay at the cashdesk).
    """
    if is_sponsored(payment_type):
        return False
    if payment_mandatory():
        return True
    return paystack_enabled() and clinic_fee(doctor) > 0


def private_booking_problem(doctor):
    """
    Why a private patient can't book this doctor online right now, or "".
    Online private bookings need Paystack switched on and a fee on the clinic.
    """
    if not paystack_enabled():
        return (
            "Online booking for private patients is unavailable right now because online payment "
            "isn't set up. Please call the hospital to book, or book as an HMO patient."
        )
    if clinic_fee(doctor) <= 0:
        clinic = doctor.department.name if doctor is not None and doctor.department_id else "this clinic"
        return (
            f"Private online booking for {clinic} opens once the hospital sets its consultation fee. "
            "Please call the hospital to book."
        )
    return ""


def to_kobo(amount):
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------
# Holds
# ------------------------------------------------------------------

def expire_payment_holds():
    """Mark unpaid online bookings whose hold has run out. Returns the count."""
    now = timezone.now()
    return Booking.objects.filter(
        status__iexact=AWAITING_PAYMENT, payment_expires_at__lte=now
    ).update(
        status=PAYMENT_EXPIRED,
        delete_reason="Online payment was not completed in time; the slot was released.",
        updated_at=now,
    )


# ------------------------------------------------------------------
# Appointment times and the HMO notice rule
# ------------------------------------------------------------------

def appointment_start(date_value, time_value="", doctor=None):
    """Aware datetime (hospital time zone) when the appointment starts."""
    day = parse_date(date_value)
    if day is None:
        return None
    t = parse_time_of_day(time_value)
    if t is None and doctor is not None:
        t = parse_time_of_day(resolve_doctor_day(doctor, day).get("shift") or "")
    if t is None:
        t = datetime.time(8, 0)
    return timezone.make_aware(datetime.datetime.combine(day, t), timezone.get_current_timezone())


# HMO patients book online inside a window: it opens HMO_MIN_NOTICE_HOURS (48)
# before the clinic starts and closes HMO_CLOSE_MINUTES before it starts, while
# space remains. Private patients can book open spaces at any time.
HMO_CLOSE_MINUTES = 10


def hmo_window(start):
    """(opens_at, closes_at) of the HMO booking window for a clinic starting at `start`."""
    return (
        start - datetime.timedelta(hours=hmo_notice_hours()),
        start - datetime.timedelta(minutes=HMO_CLOSE_MINUTES),
    )


def hmo_window_state(start, now=None):
    """"open", "not_yet" (window opens later) or "closed" (too close to / after the start)."""
    if start is None:
        return "closed"
    now = now or timezone.now()
    opens, closes = hmo_window(start)
    if now < opens:
        return "not_yet"
    if now > closes:
        return "closed"
    return "open"


def hmo_too_early(date_value, time_value="", doctor=None):
    """When HMO booking for this clinic opens, if that is still in the future; else None."""
    start = appointment_start(date_value, time_value, doctor)
    if start is not None and hmo_window_state(start) == "not_yet":
        return hmo_window(start)[0]
    return None


def _clinic_days(doctor, exclude_ref=None, days=120):
    today = timezone.localdate()
    for offset in range(days):
        day = today + datetime.timedelta(days=offset)
        resolved = resolve_doctor_day(doctor, day)
        if not resolved.get("on_duty"):
            continue
        start = appointment_start(day, resolved.get("shift") or "", doctor)
        if start is None:
            continue
        if count_doctor_bookings(doctor.doc_id, day, exclude_ref=exclude_ref) >= resolved["capacity"]:
            continue
        yield {"date": day.isoformat(), "time_window": resolved.get("shift") or "", "start": start,
               "opens": hmo_window(start)[0]}


def earliest_hmo_slot(doctor, exclude_ref=None):
    """A clinic day (with space) an HMO patient can book with this doctor right now, or None."""
    horizon = hmo_notice_hours() // 24 + 2
    for slot in _clinic_days(doctor, exclude_ref, days=horizon):
        if hmo_window_state(slot["start"]) == "open":
            return slot
    return None


def next_hmo_opening(doctor, exclude_ref=None):
    """The next clinic day (with space) whose HMO window hasn't opened yet, or None."""
    for slot in _clinic_days(doctor, exclude_ref):
        if hmo_window_state(slot["start"]) == "not_yet":
            return slot
    return None


def _when(dt):
    local = timezone.localtime(dt)
    return local.strftime("%A, %d %B %Y, %I:%M %p").replace(" 0", " ")


def hmo_notice_message(doctor_label, opens_at, bookable_now=None, clinic_start=None, sponsor="HMO"):
    """sponsor: "HMO" or "company" (Company patients follow the same window)."""
    hours = hmo_notice_hours()
    who = "HMO" if sponsor == "HMO" else "Company"
    yours = "your HMO" if sponsor == "HMO" else "your company"
    text = (
        f"{who} patients can book a clinic from {hours} hours before it starts until {HMO_CLOSE_MINUTES} minutes "
        f"before it starts, if space is still available. This gives us time to get approval from {yours}. "
        + (f"{who} booking for the {doctor_label} clinic on {timezone.localtime(clinic_start).strftime('%A, %d %B').replace(' 0', ' ')} "
           if clinic_start else f"{who} booking for this {doctor_label} clinic ")
        + f"opens on {_when(opens_at)}."
    )
    if bookable_now:
        text += f" The next clinic you can book with {yours} now is {timezone.localtime(bookable_now['start']).strftime('%A, %d %B %Y').replace(' 0', ' ')}."
    return text + (
        " If you can't wait, book as Private Self-Pay and pay online now. Private patients can book "
        f"any open space at any time, so this space may be taken before {who} booking opens."
    )


# ------------------------------------------------------------------
# Paystack API
# ------------------------------------------------------------------

def _paystack(method, path, body=None):
    url = f"{settings.PAYSTACK_API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {paystack_secret_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "IsaluBooking/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except Exception:
            payload = {}
        logger.warning("Paystack %s %s failed: %s %s", method, path, exc.code, payload.get("message"))
        if exc.code == 401:
            logger.error("Paystack rejected PAYSTACK_SECRET_KEY. Run: python manage.py check_payments")
            raise PaymentError(
                "Online payment is temporarily unavailable, so we can't take your payment right now. "
                "Please try again later or call the hospital to book."
            )
        raise PaymentError(payload.get("message") or "The payment service rejected the request. Please try again.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Paystack %s %s unreachable: %s", method, path, exc)
        raise PaymentError("We couldn't reach the payment service. Check your connection and try again.")
    if not payload.get("status"):
        raise PaymentError(payload.get("message") or "The payment service could not process this request.")
    return payload.get("data") or {}


def valid_webhook_signature(raw_body, signature):
    if not paystack_enabled() or not signature:
        return False
    expected = hmac.new(paystack_secret_key().encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


def _patient_email(booking):
    email = (booking.patient_email or "").strip()
    if email:
        return email
    digits = "".join(ch for ch in booking.patient_phone or "" if ch.isdigit()) or booking.ref_code.lower()
    return f"patient-{digits}@isaluhospitals.com"


def open_transaction(booking, callback_url):
    """Start (or restart) a Paystack transaction for a held booking."""
    if str(booking.status).lower() != AWAITING_PAYMENT.lower():
        raise PaymentError("This booking is not waiting for payment.")
    if not booking.payment_expires_at or booking.payment_expires_at <= timezone.now():
        raise PaymentError("Your 30-minute hold on this slot has ended. Please start the booking again.")

    reference = f"ISALU-{booking.ref_code[-10:]}-{secrets.token_hex(3).upper()}"
    data = _paystack("POST", "/transaction/initialize", {
        "email": _patient_email(booking),
        "amount": to_kobo(booking.fee_amount),
        "currency": "NGN",
        "reference": reference,
        "callback_url": callback_url,
        "metadata": {
            "ref_code": booking.ref_code,
            "patient_name": booking.patient_name,
            "clinic": booking.doctor_specialty,
            "appointment_date": booking.date,
            "cancel_action": callback_url + ("&" if "?" in callback_url else "?") + f"cancelled={booking.ref_code}",
            "custom_fields": [
                {"display_name": "Booking reference", "variable_name": "ref_code", "value": booking.ref_code},
                {"display_name": "Clinic", "variable_name": "clinic", "value": booking.doctor_specialty},
            ],
        },
    })
    booking.payment_reference = data.get("reference") or reference
    booking.save(update_fields=["payment_reference"])
    return {
        "authorization_url": data.get("authorization_url"),
        "access_code": data.get("access_code"),
        "reference": booking.payment_reference,
    }


# ------------------------------------------------------------------
# Confirmation (verify endpoint and webhook share this)
# ------------------------------------------------------------------

def _booking_for_reference(reference):
    return Booking.objects.select_for_update().filter(payment_reference=reference).first()


def confirm_payment(reference, transaction_data=None):
    """
    Confirm the booking paid with this Paystack reference.

    `transaction_data` is the verified transaction (from the webhook); when it
    is missing the transaction is fetched from Paystack. Safe to call twice.
    Returns (booking, outcome) where outcome is one of
    "paid", "already_paid", "pending", "failed", "refund_due", "not_found".
    """
    reference = str(reference or "").strip()
    if not reference:
        return None, "not_found"

    if transaction_data is None:
        transaction_data = _paystack("GET", f"/transaction/verify/{urllib.parse.quote(reference)}")

    with transaction.atomic():
        booking = _booking_for_reference(reference)
        if booking is None:
            return None, "not_found"
        if booking.payment_status == PAYMENT_STATUS_PAID and booking.paid_at:
            return booking, "already_paid"
        if booking.payment_status == PAYMENT_STATUS_REFUND:
            return booking, "refund_due"

        tx_status = str(transaction_data.get("status") or "").lower()
        if tx_status != "success":
            return booking, ("failed" if tx_status in ("failed", "reversed") else "pending")

        expected = to_kobo(booking.fee_amount or 0)
        paid_kobo = int(transaction_data.get("amount") or 0)
        if str(transaction_data.get("currency") or "NGN").upper() != "NGN" or paid_kobo < expected:
            logger.error("Paystack amount mismatch for %s: paid %s, expected %s", reference, paid_kobo, expected)
            return booking, "failed"

        amount = (Decimal(paid_kobo) / 100).quantize(Decimal("0.01"))
        paid_at = timezone.now()
        common = {
            "amount_paid": amount,
            "paid_at": paid_at,
            "payment_method": ONLINE_METHOD,
            "invoice_ref": reference,
        }

        # Paid in time, or paid late but the slot is still free: confirm.
        still_held = booking.counts_toward_capacity
        if not still_held:
            doctor = Doctor.objects.filter(doc_id=booking.doctor_id).first()
            day = parse_date(booking.date)
            capacity = resolve_doctor_day(doctor, day)["capacity"] if doctor and day else 0
            still_held = bool(doctor) and count_doctor_bookings(booking.doctor_id, booking.date, exclude_ref=booking.ref_code) < capacity

        if still_held:
            for k, v in common.items():
                setattr(booking, k, v)
            booking.status = "Confirmed"
            booking.payment_status = PAYMENT_STATUS_PAID
            booking.delete_reason = ""
            booking.payment_expires_at = None
            booking.save()
            return booking, "paid"

        # Money received but someone else took the slot after the hold ended.
        for k, v in common.items():
            setattr(booking, k, v)
        booking.status = PAYMENT_EXPIRED
        booking.payment_status = PAYMENT_STATUS_REFUND
        booking.delete_reason = (
            "Paid online after the slot hold ended and the clinic was full. "
            "Refund the patient or book them another date."
        )
        booking.save()
        return booking, "refund_due"
