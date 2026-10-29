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
PAYMENT_STATUS_REFUNDED = "Refunded"
PAYMENT_STATUS_REVERSED = "Reversed"
PAYMENT_STATUS_DISPUTED = "Disputed"


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


FIRST_VISIT = "First Visit"
FOLLOW_UP = "Follow-up"


def normalize_visit_type(value):
    """'follow up', 'Follow-Up', 'followup', 'return' -> 'Follow-up'; anything else -> 'First Visit'."""
    v = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    return FOLLOW_UP if v.replace(" ", "") in ("followup", "follow", "return", "returning", "review") else FIRST_VISIT


def first_visit_fee(doctor):
    """The clinic's first-visit consultation fee in Naira (Decimal), or 0."""
    if doctor is None or not doctor.department_id:
        return Decimal("0")
    fee = doctor.department.consultation_fee or Decimal("0")
    return Decimal(fee).quantize(Decimal("0.01"))


def follow_up_fee(doctor):
    """The clinic's follow-up fee; the first-visit fee when no follow-up fee is set."""
    first = first_visit_fee(doctor)
    if doctor is None or not doctor.department_id:
        return first
    fee = Decimal(doctor.department.follow_up_fee or 0).quantize(Decimal("0.01"))
    return fee if fee > 0 else first


def follow_up_balance(doctor):
    """What a follow-up patient pays on arrival if it turns out to be a first visit."""
    return max(first_visit_fee(doctor) - follow_up_fee(doctor), Decimal("0"))


def clinic_fee(doctor, visit_type=None):
    """Fee to pay online for this visit type (first visit by default), in Naira."""
    if normalize_visit_type(visit_type) == FOLLOW_UP:
        return follow_up_fee(doctor)
    return first_visit_fee(doctor)


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
        f"{who} patients can book a clinic from {hours} hours before it starts, if space is still available. "
        f"This gives us time to get approval from {yours}. "
        + (f"{who} booking for the {doctor_label} clinic on {timezone.localtime(clinic_start).strftime('%A, %d %B').replace(' 0', ' ')} "
           if clinic_start else f"{who} booking for this {doctor_label} clinic ")
        + f"opens on {_when(opens_at)}."
    )
    if bookable_now:
        text += f" The next clinic you can book with {yours} now is {timezone.localtime(bookable_now['start']).strftime('%A, %d %B %Y').replace(' 0', ' ')}."
    return text


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
    # Booking for someone else: the person making the booking is the one paying.
    if getattr(booking, "booked_for_other", False) and (booking.booker_email or "").strip():
        return booking.booker_email.strip()
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
    from .models import PaymentAttempt
    PaymentAttempt.objects.get_or_create(
        reference=booking.payment_reference,
        defaults={"booking_ref": booking.ref_code, "amount_kobo": to_kobo(booking.fee_amount), "currency": "NGN"},
    )
    return {
        "authorization_url": data.get("authorization_url"),
        "access_code": data.get("access_code"),
        "reference": booking.payment_reference,
    }


# ------------------------------------------------------------------
# Confirmation (verify endpoint and webhook share this)
# ------------------------------------------------------------------

def tx_ref_code(tx):
    """The booking reference Paystack carries in the transaction's metadata."""
    meta = (tx or {}).get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    if not isinstance(meta, dict):
        return ""
    if meta.get("ref_code"):
        return str(meta["ref_code"]).strip()
    for field in meta.get("custom_fields") or []:
        if isinstance(field, dict) and field.get("variable_name") == "ref_code" and field.get("value"):
            return str(field["value"]).strip()
    return ""


def booking_ref_for(reference, tx=None):
    """
    Which booking a Paystack reference paid for: the booking's current
    reference, any earlier attempt (the patient pressed Pay twice), or the
    booking reference Paystack carries in the metadata.
    """
    from .models import PaymentAttempt

    b = Booking.objects.filter(payment_reference=reference).values_list("ref_code", flat=True).first()
    if b:
        return b
    b = Booking.objects.filter(invoice_ref=reference, payment_method=ONLINE_METHOD).values_list("ref_code", flat=True).first()
    if b:
        return b
    attempt = PaymentAttempt.objects.filter(reference=reference).values_list("booking_ref", flat=True).first()
    if attempt:
        return attempt
    code = tx_ref_code(tx)
    if code and Booking.objects.filter(ref_code=code).exists():
        return code
    return None


def _note_attempt(reference, ref_code, tx):
    from .models import PaymentAttempt

    status = str(tx.get("status") or "").lower() or "unknown"
    paid_at = None
    if status == "success" and (tx.get("paid_at") or tx.get("paidAt")):
        from django.utils.dateparse import parse_datetime
        paid_at = parse_datetime(str(tx.get("paid_at") or tx.get("paidAt")))
    auth = tx.get("authorization") or {}
    fields = {
        "status": status[:20],
        "paystack_id": str(tx.get("id") or "")[:40],
        "paid_kobo": int(tx.get("amount") or 0) if status == "success" else None,
        "fees_kobo": int(tx["fees"]) if tx.get("fees") not in (None, "") else None,
        "channel": str(tx.get("channel") or auth.get("channel") or "")[:40],
        "currency": str(tx.get("currency") or "NGN")[:5],
        "last_checked_at": timezone.now(),
    }
    if paid_at:
        fields["paid_at"] = paid_at
    attempt, created = PaymentAttempt.objects.get_or_create(
        reference=reference, defaults={"booking_ref": ref_code or "", "amount_kobo": int(tx.get("amount") or 0), **fields})
    if not created:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(**fields)


def confirm_payment(reference, transaction_data=None, source="verify", actor="", ip=""):
    """
    Confirm the booking paid with this Paystack reference.

    `transaction_data` is the verified transaction; when it is missing it is
    fetched from Paystack. Safe to call twice, and from several places at once
    (browser return, webhook, automatic check, staff): the booking row is
    locked while it is updated. Returns (booking, outcome), outcome one of
    "paid", "already_paid", "duplicate", "pending", "failed", "underpaid",
    "refund_due", "not_found".

    Every outcome that involves money is written to the ledger
    (PaymentEvent), and money owed back opens a Refund.
    """
    from . import payment_ledger as ledger

    reference = str(reference or "").strip()
    if not reference:
        return None, "not_found"

    # A reschedule service charge, not a booking payment.
    from .reschedule_pay import confirm_reschedule, is_reschedule_reference
    if is_reschedule_reference(reference):
        return confirm_reschedule(reference, transaction_data, source=source, actor=actor, ip=ip)

    if transaction_data is None:
        transaction_data = _paystack("GET", f"/transaction/verify/{urllib.parse.quote(reference, safe='')}")
    tx = transaction_data or {}
    tx_status = str(tx.get("status") or "").lower()
    paid_kobo = int(tx.get("amount") or 0)
    paid_naira = (Decimal(paid_kobo) / 100).quantize(Decimal("0.01"))

    ref_code = booking_ref_for(reference, tx)
    _note_attempt(reference, ref_code, tx)

    if ref_code is None:
        if tx_status == "success" and (reference.upper().startswith("ISALU-") or tx_ref_code(tx)):
            ledger.record("unmatched_payment", reference=reference, amount=paid_naira, source=source, actor=actor, ip=ip,
                          note="Paystack received this payment but no booking matches it.", once=True)
        return None, "not_found"

    with transaction.atomic():
        booking = Booking.objects.select_for_update().filter(ref_code=ref_code).first()
        if booking is None:
            return None, "not_found"

        paid_ref = (booking.invoice_ref or "") if booking.payment_method == ONLINE_METHOD else ""
        already_paid = booking.payment_status in (PAYMENT_STATUS_PAID, PAYMENT_STATUS_DISPUTED) and booking.paid_at

        if tx_status != "success":
            if already_paid:
                return booking, "already_paid"
            return booking, ("failed" if tx_status in ("failed", "reversed") else "pending")

        # ---- Paystack says this reference was paid ----------------------------
        if already_paid or booking.payment_status in (PAYMENT_STATUS_REFUND, PAYMENT_STATUS_REFUNDED):
            if reference == paid_ref or (booking.payment_status == PAYMENT_STATUS_REFUND and reference == booking.payment_reference):
                return booking, ("already_paid" if already_paid else "refund_due")
            # A second payment for a booking that is already settled.
            if ledger.record("duplicate_payment", reference=reference, booking=booking, amount=paid_naira, source=source,
                             actor=actor, ip=ip, once=True,
                             note=f"Booking already settled ({booking.payment_status}, {booking.payment_method or 'unknown'}"
                                  + (f", {paid_ref}" if paid_ref else "") + "). Refund this payment."):
                ledger.open_refund(reference=reference, booking=booking, amount=paid_naira, reason="duplicate",
                                   note="Second payment for the same booking.")
            return booking, "duplicate"

        expected = to_kobo(booking.fee_amount or 0)
        if str(tx.get("currency") or "NGN").upper() != "NGN" or paid_kobo < expected:
            logger.error("Paystack amount mismatch for %s: paid %s, expected %s", reference, paid_kobo, expected)
            if ledger.record("underpaid", reference=reference, booking=booking, amount=paid_naira, source=source,
                             actor=actor, ip=ip, once=True,
                             note=f"Paid {paid_naira} {tx.get('currency') or 'NGN'}; the clinic fee is "
                                  f"{Decimal(expected) / 100:.2f} NGN. Refund it or collect the balance at the Cashdesk."):
                ledger.open_refund(reference=reference, booking=booking, amount=paid_naira, reason="underpaid",
                                   note="Paid less than the clinic fee.")
            return booking, "underpaid"

        # The booking records the clinic's consultation fee exactly. If the Paystack
        # account passes its transaction charges to customers, the amount charged is
        # higher; those charges aren't part of the consultation fee.
        amount = Decimal(booking.fee_amount or 0).quantize(Decimal("0.01"))
        if paid_kobo > expected:
            logger.info("Paystack %s: charged %s kobo for a %s kobo fee (customer-paid charges)", reference, paid_kobo, expected)
        common = {
            "amount_paid": amount,
            "paid_at": timezone.now(),
            "payment_method": ONLINE_METHOD,
            "invoice_ref": reference,
            "payment_reference": reference,
        }

        # Paid in time, or paid late but the slot is still free: confirm.
        still_held = booking.counts_toward_capacity
        if not still_held:
            doctor = Doctor.objects.filter(doc_id=booking.doctor_id).first()
            day = parse_date(booking.date)
            capacity = resolve_doctor_day(doctor, day)["capacity"] if doctor and day else 0
            still_held = bool(doctor) and count_doctor_bookings(booking.doctor_id, booking.date, exclude_ref=booking.ref_code) < capacity

        for k, v in common.items():
            setattr(booking, k, v)
        if still_held:
            booking.status = "Confirmed"
            booking.payment_status = PAYMENT_STATUS_PAID
            booking.delete_reason = ""
            booking.payment_expires_at = None
            booking.save()
            ledger.record("payment_confirmed", reference=reference, booking=booking, amount=amount, source=source,
                          actor=actor, ip=ip, data={"charged": float(paid_naira), "fees": (int(tx["fees"]) / 100) if tx.get("fees") else None})
            ledger.notify_patient(booking.ref_code, "paid", amount=amount)
            return booking, "paid"

        # Money received but someone else took the slot after the hold ended.
        booking.status = PAYMENT_EXPIRED
        booking.payment_status = PAYMENT_STATUS_REFUND
        booking.delete_reason = (
            "Paid online after the slot hold ended and the clinic was full. "
            "Refund the patient or book them another date."
        )
        booking.save()
        ledger.record("paid_clinic_full", reference=reference, booking=booking, amount=paid_naira, source=source,
                      actor=actor, ip=ip, once=True, note="Paid after the hold ended; the clinic had filled.")
        ledger.open_refund(reference=reference, booking=booking, amount=paid_naira, reason="clinic_full",
                           note="Paid after the clinic filled.")
        ledger.notify_patient(booking.ref_code, "refund_due", amount=paid_naira)
        return booking, "refund_due"
