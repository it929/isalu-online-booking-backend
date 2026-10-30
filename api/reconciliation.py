"""
Payment Reconciliation: staff check a private patient's online payment
directly with Paystack, using the Paystack reference (or the booking
reference), and compare it with the booking.

Paystack is the source of truth. Confirming a booking goes through the same
`payments.confirm_payment()` the booking page, the webhook and the automatic
check use, so the amount and slot checks are identical and the booking row is
locked while it changes.

Who can do what
- Check a payment: anyone with the module (Helpdesk, Cashdesk, Private
  Self-Pay List, Booking Payments, Cleared Patients, administrators).
- Confirm a booking from a payment, record a refund paid back by transfer or
  cash, cancel a refund: Cashdesk, Booking Payments, administrators.
- Send a refund through Paystack: Booking Payments and administrators.
- Open a new refund by hand, run the automatic check now: administrators.

Every check is written to PaymentCheck; every money change to PaymentEvent.
"""

import datetime
import logging
import re
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import payment_ops as ops
from . import payments
from .models import Booking, Doctor, PaymentAttempt, PaymentCheck, PaymentEvent, Refund
from .scheduling import AWAITING_PAYMENT, PAYMENT_EXPIRED

logger = logging.getLogger(__name__)

UNRESOLVED_DAYS = 30
REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,99}$")


class ReconcileError(Exception):
    """A problem staff can act on (shown in the dashboard)."""


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def _has(user, desk):
    from .views import user_has_desk
    return user_has_desk(user, desk)


def _admin(user):
    from .views import is_admin_user
    return is_admin_user(user)


def permissions_for(user):
    """
    - Helpdesk (and other front-desk roles): check a payment's status only;
      no money figures, refunds, daily reconciliation, ledger or activity.
    - Cashdesk: also confirm payments, record refunds, see the money.
    - Finance (and Booking Payments): every financial feature.
    """
    admin = _admin(user)
    finance = admin or _has(user, "finance") or _has(user, "booking_payments")
    money = finance or _has(user, "cashdesk")
    return {
        "check": admin or _has(user, "payment_reconciliation"),
        "view_money": money,
        "confirm": money,
        "record_refund": money,
        "paystack_refund": finance,
        "create_refund": finance,
        "run_check": finance,
    }


def _strip_money(result):
    """Status-only view of a check result, for roles without financial access."""
    tx = result.get("paystack")
    if tx:
        result["paystack"] = {k: tx.get(k) for k in ("reference", "status", "channel", "paid_at", "created_at", "booking_ref", "amount", "currency")}
    for k in ("refunds", "events", "attempts", "suggestions"):
        result[k] = []
    result["claimed"] = None
    return result


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _naira(kobo):
    try:
        return float(Decimal(int(kobo or 0)) / 100)
    except Exception:
        return 0.0


def _iso(value):
    if not value:
        return None
    dt = value if isinstance(value, datetime.datetime) else parse_datetime(str(value))
    if dt is None:
        return str(value)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)
    return timezone.localtime(dt).isoformat()


def _tx_payload(tx):
    auth = tx.get("authorization") or {}
    customer = tx.get("customer") or {}
    amount = _naira(tx.get("amount"))
    fees = _naira(tx.get("fees"))
    return {
        "id": tx.get("id"),
        "reference": tx.get("reference"),
        "status": str(tx.get("status") or "").lower(),
        "gateway_response": tx.get("gateway_response") or "",
        "amount": amount,
        "fees": fees,
        "net": round(amount - fees, 2) if tx.get("fees") is not None else None,
        "currency": tx.get("currency") or "NGN",
        "channel": tx.get("channel") or auth.get("channel") or "",
        "card": " ".join(x for x in [str(auth.get("card_type") or "").strip().title(),
                                     f"•••• {auth['last4']}" if auth.get("last4") else ""] if x),
        "bank": auth.get("bank") or "",
        "customer_email": customer.get("email") or "",
        "paid_at": _iso(tx.get("paid_at") or tx.get("paidAt")),
        "created_at": _iso(tx.get("created_at") or tx.get("createdAt")),
        "booking_ref": payments.tx_ref_code(tx),
    }


def _booking_payload(b):
    return {
        "ref_code": b.ref_code,
        "patient_name": b.patient_name,
        "patient_phone": b.patient_phone,
        "patient_email": b.patient_email or "",
        "clinic": b.doctor_specialty,
        "doctor": b.doctor_name,
        "date": b.date,
        "time": b.time,
        "visit_type": getattr(b, "visit_type", "") or "",
        "payment_type": b.payment_type,
        "payment_method": b.payment_method,
        "status": b.status,
        "payment_status": b.payment_status,
        "fee_amount": float(b.fee_amount) if b.fee_amount is not None else None,
        "amount_paid": float(b.amount_paid) if b.amount_paid is not None else None,
        "paid_at": _iso(b.paid_at),
        "payment_reference": b.payment_reference or "",
        "paid_reference": b.invoice_ref if b.payment_method == payments.ONLINE_METHOD else "",
        "payment_expires_at": _iso(b.payment_expires_at),
        "receipt_sent_at": _iso(getattr(b, "payment_receipt_sent_at", None)),
        "hospital_card_number": getattr(b, "hospital_card_number", "") or "",
        "booked_for_other": bool(getattr(b, "booked_for_other", False)),
        "booker_name": getattr(b, "booker_name", "") or "",
        "booker_phone": getattr(b, "booker_phone", "") or "",
        "booker_email": getattr(b, "booker_email", "") or "",
        "note": b.delete_reason or "",
        "created_at": _iso(getattr(b, "created_at", None)),
    }


def event_payload(e):
    return {
        "id": e.id, "kind": e.kind, "label": dict(PaymentEvent.KINDS).get(e.kind, e.kind),
        "reference": e.reference, "ref_code": e.ref_code, "patient_name": e.patient_name,
        "amount": float(e.amount) if e.amount is not None else None,
        "source": e.source, "actor": e.actor, "note": e.note, "created_at": _iso(e.created_at),
    }


def _attempt_payload(a):
    return {"reference": a.reference, "status": a.status, "amount": _naira(a.amount_kobo),
            "paid": _naira(a.paid_kobo) if a.paid_kobo is not None else None,
            "created_at": _iso(a.created_at), "paid_at": _iso(a.paid_at), "last_checked_at": _iso(a.last_checked_at)}


# ---------------------------------------------------------------------------
# Matching and verdicts
# ---------------------------------------------------------------------------

def _find_booking(query):
    q = str(query or "").strip()
    b = Booking.objects.filter(payment_reference__iexact=q).first()
    if b:
        return b, "payment_reference"
    b = Booking.objects.filter(ref_code__iexact=q).first()
    if b:
        return b, "ref_code"
    b = Booking.objects.filter(invoice_ref__iexact=q).first()
    if b:
        return b, "invoice_ref"
    a = PaymentAttempt.objects.filter(reference__iexact=q).first()
    if a:
        b = Booking.objects.filter(ref_code=a.booking_ref).first()
        if b:
            return b, "attempt"
    return None, ""


def _is_cleared(b):
    return str(b.payment_status or "").strip().lower() == payments.PAYMENT_STATUS_PAID.lower()


def _is_private(b):
    return not payments.is_sponsored(b.payment_type)


VERDICTS = {
    "confirmed": ("success", "Paid and confirmed"),
    "paid_not_confirmed": ("warning", "Paid on Paystack, but the booking is not confirmed"),
    "refund_due": ("warning", "Paid, but the slot was taken: refund due"),
    "refunded": ("neutral", "Paid and refunded"),
    "amount_mismatch": ("danger", "Paid less than the clinic fee"),
    "duplicate_payment": ("warning", "Extra payment: this booking was already paid"),
    "not_paid": ("neutral", "Not paid"),
    "reversed": ("danger", "Payment reversed"),
    "disputed": ("danger", "Payment disputed by the patient's bank"),
    "cleared_but_unpaid": ("danger", "Booking shows cleared, but Paystack has no successful payment"),
    "other_attempt": ("neutral", "This attempt wasn't completed; the booking was paid another way"),
    "no_booking": ("neutral", "Paystack payment with no matching booking"),
    "not_on_paystack": ("danger", "Paystack has no payment with this reference"),
    "no_online_payment": ("neutral", "This booking has no online payment"),
    "reschedule_applied": ("success", "Reschedule fee paid; booking moved"),
    "reschedule_pending": ("warning", "Reschedule fee paid, but the booking hasn't moved yet"),
    "reschedule_not_applied": ("warning", "Reschedule fee paid, but the new date wasn't free: refund due"),
    "reschedule_unpaid": ("neutral", "Reschedule fee not paid"),
    "unmatched_dismissed": ("neutral", "Marked as not a booking payment"),
}


def _reschedule_verdict(rp, tx):
    status = str((tx or {}).get("status") or "").lower()
    if tx is None or status != "success":
        return "reschedule_unpaid", (f"The patient started a reschedule to {rp.new_date} but Paystack shows "
                                     f"'{status or 'no payment'}'. The appointment stays on {rp.old_date}."), False
    if rp.status == "applied":
        return "reschedule_applied", (f"Paid ₦{float(rp.amount):,.2f} ({rp.percent}% of the clinic fee). The appointment "
                                      f"moved from {rp.old_date} to {rp.new_date}."), False
    if rp.status in ("not_applied", "failed"):
        return "reschedule_not_applied", (rp.note or "The reschedule couldn't be applied.") + " A refund has been opened.", False
    return "reschedule_pending", (f"Paystack confirms the reschedule fee, but the appointment hasn't moved to {rp.new_date} yet. "
                                  "Confirm to move it now."), True


def _verdict(tx, booking, reference):
    """(key, explanation, can_confirm)."""
    from .models import ReschedulePayment
    rp = ReschedulePayment.objects.filter(reference=reference).first()
    if rp is not None:
        return _reschedule_verdict(rp, tx)
    if tx is None:
        if booking and not booking.payment_reference and booking.payment_method != payments.ONLINE_METHOD:
            how = "an HMO/company patient" if not _is_private(booking) else "not paid online"
            return "no_online_payment", (
                f"Booking {booking.ref_code} was {how}, so there is no Paystack payment to check. "
                "Private patients who pay at the hospital are cleared at the Cashdesk."
            ), False
        return "not_on_paystack", (
            "Paystack has no payment with this reference. Check it was typed correctly (copy it from the patient's "
            "Paystack receipt or bank alert). If the patient was debited, ask for the receipt and contact Paystack."
        ), False

    status = str(tx.get("status") or "").lower()
    amount_kobo = int(tx.get("amount") or 0)

    if booking is None:
        if status == "success":
            dismissed = PaymentEvent.objects.filter(kind="unmatched_dismissed", reference=reference).first()
            if dismissed:
                return "unmatched_dismissed", (f"{dismissed.actor or 'Staff'} marked this payment as not a booking payment "
                                               f"on {timezone.localtime(dismissed.created_at):%d %b %Y}: {dismissed.note}"), False
            claimed = payments.tx_ref_code(tx)
            if claimed:
                why = (f"Paystack received ₦{_naira(amount_kobo):,.2f} for booking {claimed}, but that booking isn't on this "
                       "system. This usually means the payment was made on another copy of the booking site that uses the "
                       "same Paystack keys (for example a test or local copy), or the booking was created elsewhere. ")
            else:
                why = (f"Paystack received ₦{_naira(amount_kobo):,.2f} with this reference, but it carries no booking "
                       "reference and no booking on this system matches it. ")
            return "no_booking", why + ("Link it to the patient's booking below, open a refund, or mark it as not a booking "
                                        "payment."), False
        return "no_booking", "Paystack has this reference, but it wasn't paid and no booking matches it.", False

    paid_ref = booking.invoice_ref if booking.payment_method == payments.ONLINE_METHOD else ""
    this_paid = bool(paid_ref) and paid_ref.lower() == reference.lower()
    ps = str(booking.payment_status or "")

    if status == "reversed" or (this_paid and ps == payments.PAYMENT_STATUS_REVERSED):
        return "reversed", ("Paystack reversed this payment (the money went back to the patient). The booking is not "
                            "treated as paid; collect payment at the Cashdesk before the patient is seen."), False

    if status == "success":
        if this_paid and ps == payments.PAYMENT_STATUS_DISPUTED:
            return "disputed", ("The patient's bank opened a dispute on this payment. Respond on the Paystack dashboard. "
                                "Until it's resolved the booking is not treated as paid."), False
        if this_paid and ps == payments.PAYMENT_STATUS_REFUNDED:
            return "refunded", booking.delete_reason or "This payment was refunded to the patient.", False
        if ps == payments.PAYMENT_STATUS_REFUND and (this_paid or booking.payment_reference.lower() == reference.lower()):
            return "refund_due", (booking.delete_reason or "The patient paid after the slot hold ended and the clinic was "
                                  "full. Refund the patient or book them another date."), False
        if _is_cleared(booking) or ps in (payments.PAYMENT_STATUS_REFUND, payments.PAYMENT_STATUS_REFUNDED,
                                           payments.PAYMENT_STATUS_DISPUTED):
            if this_paid:
                return "confirmed", (f"Paystack confirms the payment and booking {booking.ref_code} is confirmed and "
                                     "cleared. Nothing to do."), False
            how = f"reference {paid_ref}" if paid_ref else f"at the Cashdesk ({booking.payment_method or 'paid in person'})"
            return "duplicate_payment", (f"Booking {booking.ref_code} was already paid with {how}. This is a second "
                                         "payment for the same booking: refund it to the patient."), False
        expected = payments.to_kobo(booking.fee_amount or 0)
        if str(tx.get("currency") or "NGN").upper() != "NGN" or (expected and amount_kobo < expected):
            return "amount_mismatch", (
                f"Paystack received ₦{_naira(amount_kobo):,.2f} but the clinic fee on the booking is "
                f"₦{_naira(expected):,.2f}. The booking can't be confirmed online; collect the balance at the Cashdesk "
                "or refund the patient."
            ), False
        if not _is_private(booking):
            return "no_online_payment", "This booking is for an HMO or company patient, so it can't be confirmed from an online payment.", False
        return "paid_not_confirmed", (
            "Paystack confirms the patient paid, but the booking was never confirmed (usually the patient closed the "
            "page before returning and the confirmation from Paystack didn't arrive). Confirm the booking now."
        ), True

    if _is_cleared(booking):
        if not this_paid:
            how = f"reference {paid_ref}" if paid_ref else f"at the Cashdesk ({booking.payment_method or 'in person'})"
            return "other_attempt", (f"The patient started this payment but didn't complete it. Booking "
                                     f"{booking.ref_code} was paid with {how}."), False
        return "cleared_but_unpaid", (
            "The booking is marked cleared but Paystack has no successful payment for this reference. "
            "Ask the patient for proof of payment before they are seen."
        ), False

    label = {"abandoned": "The patient opened the payment page but didn't pay",
             "failed": "The payment failed (declined by the bank or card)",
             "pending": "The payment is still pending with the bank",
             "ongoing": "The payment is still in progress",
             "processing": "The payment is still processing",
             "queued": "The payment is queued at Paystack"}.get(status, f"Paystack status: {status or 'unknown'}")
    held = str(booking.status) == AWAITING_PAYMENT and booking.counts_toward_capacity
    tail = (" The slot is still held for the patient, so they can still pay from the booking page."
            if held else " The booking is not confirmed; the patient can book again and pay, or pay at the Cashdesk.")
    return "not_paid", label + "." + tail, False


def _meta(tx):
    meta = (tx or {}).get("metadata")
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except ValueError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _synthetic_phone(email):
    """The booking site emails Paystack 'patient-08031234567@isaluhospitals.com' when the patient gave none."""
    m = re.match(r"^patient-(\d{7,15})@isaluhospitals\.com$", str(email or "").strip().lower())
    return m.group(1) if m else ""


def _claimed(tx):
    """What the Paystack transaction says it was for (from the metadata the booking site sent)."""
    meta = _meta(tx)
    customer = (tx or {}).get("customer") or {}
    email = str(customer.get("email") or "")
    return {
        "booking_ref": payments.tx_ref_code(tx),
        "patient_name": str(meta.get("patient_name") or ""),
        "clinic": str(meta.get("clinic") or ""),
        "appointment_date": str(meta.get("appointment_date") or meta.get("new_date") or ""),
        "purpose": str(meta.get("purpose") or "booking"),
        "email": "" if _synthetic_phone(email) else email,
        "phone": _synthetic_phone(email) or str(customer.get("phone") or ""),
    }


def _suggestions(tx, limit=6):
    """Bookings that may be the one this unmatched payment was for (same name, phone or email)."""
    from .duplicates import normalize_name, normalize_phone

    c = _claimed(tx)
    name_key = normalize_name(c["patient_name"])
    phone_key = normalize_phone(c["phone"])
    email = c["email"].lower()
    q = Q(pk__in=[])
    if c["booking_ref"]:
        q |= Q(ref_code__iexact=c["booking_ref"])
    if email:
        q |= Q(patient_email__iexact=email) | Q(booker_email__iexact=email)
    if len(phone_key) >= 7:
        q |= Q(patient_phone__contains=phone_key[-4:]) | Q(booker_phone__contains=phone_key[-4:])
    if name_key:
        q |= Q(patient_name__icontains=name_key.split()[0])
    since = timezone.now() - datetime.timedelta(days=60)
    rows = []
    for b in (Booking.objects.filter(q, created_at__gte=since).exclude(payment_type="HMO Insurance")
              .exclude(payment_type__iexact="Company").order_by("-created_at")[:200]):
        score = 0
        if c["booking_ref"] and b.ref_code.lower() == c["booking_ref"].lower():
            score += 10
        if email and email in ((b.patient_email or "").lower(), (getattr(b, "booker_email", "") or "").lower()):
            score += 4
        if len(phone_key) >= 7 and phone_key in (normalize_phone(b.patient_phone), normalize_phone(getattr(b, "booker_phone", ""))):
            score += 4
        if name_key and normalize_name(b.patient_name) == name_key:
            score += 3
        if c["clinic"] and (b.doctor_specialty or "").lower() == c["clinic"].lower():
            score += 1
        if c["appointment_date"] and str(b.date) == c["appointment_date"]:
            score += 1
        if score >= 3:
            row = _booking_payload(b)
            row["score"] = score
            row["settled"] = b.payment_status in (payments.PAYMENT_STATUS_PAID, payments.PAYMENT_STATUS_REFUNDED)
            rows.append(row)
    rows.sort(key=lambda r: (-r["score"], r["settled"]))
    return rows[:limit]


def _result(reference, tx, booking, how, user=None):
    key, explanation, can_confirm = _verdict(tx, booking, reference)
    tone, headline = VERDICTS[key]
    perms = permissions_for(user) if user is not None else {}
    refs = {reference}
    if booking:
        refs |= set(PaymentAttempt.objects.filter(booking_ref=booking.ref_code).values_list("reference", flat=True))
    refunds = Refund.objects.filter(reference__in=refs).order_by("-created_at")
    events = PaymentEvent.objects.filter(Q(reference__in=refs) | (Q(ref_code=booking.ref_code) if booking else Q(pk__in=[])))
    attempts = PaymentAttempt.objects.filter(booking_ref=booking.ref_code) if booking else PaymentAttempt.objects.none()
    result = {
        "query": reference,
        "reference": (tx or {}).get("reference") or reference,
        "matched_by": how,
        "verdict": key,
        "tone": tone,
        "headline": headline,
        "explanation": explanation,
        "can_confirm": bool(can_confirm and perms.get("confirm")),
        "confirmable": can_confirm,
        "paystack": _tx_payload(tx) if tx else None,
        "booking": _booking_payload(booking) if booking else None,
        "attempts": [_attempt_payload(a) for a in attempts[:20]],
        "refunds": [ops.refund_payload(r) for r in refunds[:20]],
        "events": [event_payload(e) for e in events.order_by("-created_at")[:30]],
        "claimed": _claimed(tx) if (tx and booking is None) else None,
        "suggestions": _suggestions(tx) if (tx and booking is None and key == "no_booking") else [],
        "permissions": perms,
        "checked_at": timezone.localtime().isoformat(),
    }
    if user is not None and not perms.get("view_money"):
        result = _strip_money(result)
    return result


def _log(result, user, action, note=""):
    tx = result.get("paystack") or {}
    booking = result.get("booking") or {}
    try:
        PaymentCheck.objects.create(
            reference=str(result.get("reference") or "")[:100],
            ref_code=str(booking.get("ref_code") or tx.get("booking_ref") or "")[:20],
            patient_name=str(booking.get("patient_name") or "")[:200],
            action=action,
            paystack_status=str(tx.get("status") or ("not found" if result["verdict"] == "not_on_paystack" else ""))[:30],
            amount=Decimal(str(tx["amount"])) if tx.get("amount") is not None else None,
            verdict=result["verdict"],
            note=note[:300],
            checked_by=ops._who(user)[:200],
        )
    except Exception:
        logger.exception("Couldn't record the payment check for %s", result.get("reference"))


def _clean(query):
    q = str(query or "").strip()
    if not q:
        raise ReconcileError("Enter the Paystack reference (or the booking reference).")
    if not REFERENCE_RE.match(q):
        raise ReconcileError("That doesn't look like a payment or booking reference. Use letters, numbers and dashes only.")
    return q


# ---------------------------------------------------------------------------
# Public functions (used by the views)
# ---------------------------------------------------------------------------

def check(query, user=None):
    q = _clean(query)
    payments.expire_payment_holds()

    booking, how = _find_booking(q)
    reference = q
    if booking is not None and how == "ref_code":
        reference = (booking.invoice_ref if booking.payment_method == payments.ONLINE_METHOD and booking.invoice_ref
                     else booking.payment_reference) or q

    tx = None
    try:
        if booking is None or how != "ref_code" or booking.payment_reference:
            tx = ops.verify(reference)
    except ops.OpsError as exc:
        raise ReconcileError(str(exc))
    if tx is not None:
        payments._note_attempt(reference, booking.ref_code if booking else payments.booking_ref_for(reference, tx), tx)
        if booking is None:
            code = payments.booking_ref_for(reference, tx)
            booking = Booking.objects.filter(ref_code=code).first() if code else None
            how = "paystack_metadata" if booking else how
        # A reversal Paystack reports here un-clears the booking straight away.
        if str(tx.get("status") or "").lower() == "reversed":
            ops.mark_reversed(reference, tx, source="staff", actor=ops._who(user))
            if booking:
                booking.refresh_from_db()

    result = _result(reference, tx, booking, how, user)
    _log(result, user, PaymentCheck.ACTION_CHECK)
    return result


def confirm(reference, user=None, ip=""):
    """Confirm the booking for a payment Paystack says succeeded. Returns (result, outcome, booking)."""
    reference = _clean(reference)
    payments.expire_payment_holds()
    try:
        tx = ops.verify(reference)
    except ops.OpsError as exc:
        raise ReconcileError(str(exc))
    if tx is None:
        raise ReconcileError("Paystack has no payment with this reference.")
    code = payments.booking_ref_for(reference, tx)
    booking = Booking.objects.filter(ref_code=code).first() if code else None
    key, _, can_confirm = _verdict(tx, booking, reference)
    if not can_confirm or booking is None:
        raise ReconcileError(f"This payment can't be confirmed from here: {VERDICTS[key][1].lower()}.")

    booking, outcome = payments.confirm_payment(reference, tx, source="staff", actor=ops._who(user), ip=ip)
    if booking is not None:
        booking.refresh_from_db()
    result = _result(reference, tx, booking, "payment_reference", user)
    result["outcome"] = outcome
    note = {
        "paid": "Booking confirmed from Payment Reconciliation.",
        "already_paid": "Booking was already confirmed.",
        "refund_due": "Clinic full: marked Refund Due.",
        "duplicate": "Already paid another way: refund opened.",
    }.get(outcome, outcome)
    _log(result, user, PaymentCheck.ACTION_CONFIRM, note)
    return result, outcome, booking


def _check_payload(c):
    return {
        "id": c.id, "reference": c.reference, "ref_code": c.ref_code, "patient_name": c.patient_name,
        "action": c.action, "paystack_status": c.paystack_status,
        "amount": float(c.amount) if c.amount is not None else None, "verdict": c.verdict,
        "headline": VERDICTS.get(c.verdict, ("", c.verdict))[1], "tone": VERDICTS.get(c.verdict, ("neutral", ""))[0],
        "note": c.note, "checked_by": c.checked_by, "created_at": _iso(c.created_at),
    }


def overview(user=None):
    """Everything that needs attention, refunds, the automatic check and recent activity."""
    payments.expire_payment_holds()
    now = timezone.now()
    since = now - datetime.timedelta(days=UNRESOLVED_DAYS)
    online = Q(payment_method=payments.ONLINE_METHOD) | ~Q(payment_reference="")
    private = ~(Q(payment_type="HMO Insurance") | Q(payment_type__iexact="Company"))

    unresolved = []
    qs = (Booking.objects.filter(online, private, created_at__gte=since)
          .exclude(payment_status__in=[payments.PAYMENT_STATUS_PAID, payments.PAYMENT_STATUS_REFUNDED,
                                       payments.PAYMENT_STATUS_REVERSED, payments.PAYMENT_STATUS_DISPUTED])
          .order_by("-created_at")[:200])
    for b in qs:
        row = _booking_payload(b)
        if str(b.payment_status) == payments.PAYMENT_STATUS_REFUND:
            row["state"] = "refund_due"
        elif str(b.status) == AWAITING_PAYMENT and b.counts_toward_capacity:
            row["state"] = "awaiting"
        elif str(b.status) in (AWAITING_PAYMENT, PAYMENT_EXPIRED):
            row["state"] = "expired"
        else:
            row["state"] = "unpaid"
        unresolved.append(row)

    flagged = [dict(_booking_payload(b), state=("reversed" if b.payment_status == payments.PAYMENT_STATUS_REVERSED else "disputed"))
               for b in Booking.objects.filter(payment_status__in=[payments.PAYMENT_STATUS_REVERSED, payments.PAYMENT_STATUS_DISPUTED])
               .order_by("-updated_at")[:100]]

    resolved = set(PaymentEvent.objects.filter(kind__in=["payment_linked", "unmatched_dismissed"])
                   .values_list("reference", flat=True)) | set(Refund.objects.values_list("reference", flat=True))
    alerts = [event_payload(e) for e in PaymentEvent.objects.filter(
        kind__in=["unmatched_payment", "cleared_without_payment", "underpaid", "duplicate_payment"],
        created_at__gte=now - datetime.timedelta(days=UNRESOLVED_DAYS))[:200]
        if not (e.kind == "unmatched_payment" and (e.reference in resolved or payments.booking_ref_for(e.reference)))][:50]

    open_refunds = Refund.objects.filter(status__in=Refund.OPEN_STATUSES)
    refunds_owed = open_refunds.aggregate(t=Sum("amount"))["t"] or 0

    attention = (len(flagged) + open_refunds.filter(status__in=[Refund.STATUS_DUE, Refund.STATUS_FAILED]).count()
                 + sum(1 for a in alerts if a["kind"] in ("unmatched_payment", "cleared_without_payment")))

    perms = permissions_for(user) if user is not None else {}
    if user is not None and not perms.get("view_money"):
        # Front desk: which patients' online payments are unsettled, nothing financial.
        return {
            "online_payments_enabled": payments.paystack_enabled(),
            "mode": ops.paystack_mode(),
            "permissions": perms,
            "unresolved_days": UNRESOLVED_DAYS,
            "attention_count": len(flagged),
            "unresolved": unresolved,
            "flagged": flagged,
            "alerts": [], "refunds": [], "refunds_owed": 0, "events": [], "checks": [], "sweep": None,
        }
    return {
        "online_payments_enabled": payments.paystack_enabled(),
        "mode": ops.paystack_mode(),
        "permissions": perms,
        "unresolved_days": UNRESOLVED_DAYS,
        "attention_count": attention,
        "unresolved": unresolved,
        "flagged": flagged,
        "alerts": alerts,
        "refunds": [ops.refund_payload(r) for r in Refund.objects.all()[:100]],
        "refunds_owed": float(refunds_owed),
        "events": [event_payload(e) for e in PaymentEvent.objects.exclude(kind="receipt_sent")[:60]],
        "checks": [_check_payload(c) for c in PaymentCheck.objects.all()[:50]],
        "sweep": ops.sweep_state(),
    }


def ledger(date_from, date_to):
    """Ledger entries in a date range (Lagos days), for export."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min), tz)
    end = timezone.make_aware(datetime.datetime.combine(date_to, datetime.time.min), tz) + datetime.timedelta(days=1)
    rows = PaymentEvent.objects.filter(created_at__gte=start, created_at__lt=end)
    return {"from": date_from.isoformat(), "to": date_to.isoformat(),
            "events": [event_payload(e) for e in rows[:5000]], "truncated": rows.count() > 5000}


def link_payment(reference, ref_code, note, user=None, ip=""):
    """
    Staff tie a Paystack payment that matched no booking to the patient's booking,
    then confirm it exactly like any other payment. Returns (result, outcome, booking).
    """
    reference = _clean(reference)
    ref_code = str(ref_code or "").strip().upper()
    note = str(note or "").strip()
    if not ref_code:
        raise ReconcileError("Enter the booking reference to link this payment to.")
    if len(note) < 3:
        raise ReconcileError("Say how you know this payment is for that booking (e.g. the patient's receipt or phone call).")
    from .reschedule_pay import is_reschedule_reference
    if is_reschedule_reference(reference):
        raise ReconcileError("This is a reschedule payment; it is already tied to its booking.")
    try:
        tx = ops.verify(reference)
    except ops.OpsError as exc:
        raise ReconcileError(str(exc))
    if tx is None or str(tx.get("status") or "").lower() != "success":
        raise ReconcileError("Paystack doesn't show a successful payment with this reference.")
    current = payments.booking_ref_for(reference, tx)
    if current and current.upper() != ref_code:
        raise ReconcileError(f"This payment is already tied to booking {current}.")
    booking = Booking.objects.filter(ref_code__iexact=ref_code).first()
    if booking is None:
        raise ReconcileError(f"There is no booking {ref_code} on this system.")
    if payments.is_sponsored(booking.payment_type):
        raise ReconcileError("That is an HMO or company booking; it isn't paid online.")
    if booking.payment_status in (payments.PAYMENT_STATUS_PAID, payments.PAYMENT_STATUS_REFUNDED, payments.PAYMENT_STATUS_REFUND):
        raise ReconcileError(f"Booking {booking.ref_code} is already settled ({booking.payment_status}). If the patient paid "
                             "twice, open a refund for this payment instead.")
    paid_kobo = int(tx.get("amount") or 0)
    doctor = Doctor.objects.filter(doc_id=booking.doctor_id).first()
    fee = Decimal(booking.fee_amount or (payments.clinic_fee(doctor, booking.visit_type) if doctor else 0) or 0)
    if fee <= 0:
        raise ReconcileError("That booking's clinic has no consultation fee, so there is nothing to confirm with this payment.")
    if paid_kobo < payments.to_kobo(fee):
        raise ReconcileError(f"This payment (₦{_naira(paid_kobo):,.2f}) is less than the booking's clinic fee (₦{fee:,.2f}), "
                             "so it can't confirm the booking. Open a refund or collect the balance at the Cashdesk.")
    if not booking.fee_amount:
        # Booked without online payment (e.g. pay-at-cashdesk); record the fee this payment covers.
        Booking.objects.filter(ref_code=booking.ref_code).update(fee_amount=fee, payment_method=payments.ONLINE_METHOD)
    PaymentAttempt.objects.update_or_create(reference=reference, defaults={
        "booking_ref": booking.ref_code, "amount_kobo": paid_kobo})
    from . import payment_ledger as ledger
    ledger.record("payment_linked", reference=reference, booking=booking, amount=_naira(paid_kobo), source="staff",
                  actor=ops._who(user), ip=ip, note=note[:400])
    booking, outcome = payments.confirm_payment(reference, tx, source="staff", actor=ops._who(user), ip=ip)
    if booking is not None:
        booking.refresh_from_db()
    result = _result(reference, tx, booking, "payment_linked", user)
    result["outcome"] = outcome
    _log(result, user, PaymentCheck.ACTION_CONFIRM, f"Linked to {ref_code}: {outcome}")
    return result, outcome, booking


def resolve_unmatched(reference, action, note, user=None, ip=""):
    """A payment with no booking: 'dismiss' (not a booking payment) or 'refund' (open a refund)."""
    reference = _clean(reference)
    note = str(note or "").strip()
    if len(note) < 3:
        raise ReconcileError("Add a short note explaining the decision.")
    try:
        tx = ops.verify(reference)
    except ops.OpsError as exc:
        raise ReconcileError(str(exc))
    if tx is None or str(tx.get("status") or "").lower() != "success":
        raise ReconcileError("Paystack doesn't show a successful payment with this reference.")
    if payments.booking_ref_for(reference, tx):
        raise ReconcileError("This payment matches a booking; check it instead.")
    from . import payment_ledger as ledger
    amount = _naira(tx.get("amount"))
    if action == "dismiss":
        ledger.record("unmatched_dismissed", reference=reference, amount=amount, source="staff", actor=ops._who(user),
                      ip=ip, note=note[:400], once=True)
    elif action == "refund":
        ledger.open_refund(reference=reference, booking=None, amount=amount, reason="other",
                           note=f"Payment with no booking: {note}"[:500], created_by=ops._who(user) or "staff")
    else:
        raise ReconcileError("Unknown action.")
    result = _result(reference, tx, None, "", user)
    _log(result, user, PaymentCheck.ACTION_CHECK, f"Unmatched payment: {action}")
    return result
