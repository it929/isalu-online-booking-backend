"""
Money operations behind Payment Reconciliation:

- Refunds: through Paystack, or recorded as paid back by bank transfer/cash.
  Never more than the payment, never twice, always on the ledger.
- Disputes and reversals from Paystack (webhook or automatic check).
- The automatic check (sweep) that runs every few minutes: confirms paid
  bookings nobody confirmed, catches late payments on older attempts and
  reversed payments, and records payments with no booking.
- Daily reconciliation: Paystack's money for a day against the bookings.

All Paystack calls here are staff-facing (errors explain what to do).
"""

import datetime
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import payment_ledger as ledger
from . import payments
from .models import Booking, PaymentAttempt, PaymentEvent, PaymentJobState, Refund
from .scheduling import AWAITING_PAYMENT, PAYMENT_EXPIRED

logger = logging.getLogger(__name__)

ONLINE = payments.ONLINE_METHOD


class OpsError(Exception):
    """A problem staff can act on (shown in the dashboard)."""


# ---------------------------------------------------------------------------
# Paystack (staff-facing)
# ---------------------------------------------------------------------------

def paystack_call(method, path, body=None, not_found_ok=False):
    """Returns Paystack's `data`. not_found_ok: a missing transaction returns None instead of raising."""
    if not payments.paystack_enabled():
        raise OpsError(
            "Online payment is switched off on this server (no PAYSTACK_SECRET_KEY in backend/.env), "
            "so Paystack can't be reached."
        )
    url = f"{settings.PAYSTACK_API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {payments.paystack_secret_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "IsaluBooking/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode() or "{}").get("message") or ""
        except Exception:
            message = ""
        if not_found_ok and exc.code in (400, 404) and "not found" in message.lower():
            return None
        logger.warning("Paystack %s %s failed: %s %s", method, path, exc.code, message)
        if exc.code == 401:
            raise OpsError("Paystack rejected the secret key in backend/.env. Run 'python manage.py check_payments' "
                           "to find the problem, then restart the backend.")
        raise OpsError(f"Paystack answered with an error ({exc.code}): {message or 'no details'}.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Paystack %s %s unreachable: %s", method, path, exc)
        raise OpsError("Couldn't reach Paystack. Check the server's internet connection and try again.")
    if not payload.get("status"):
        message = str(payload.get("message") or "")
        if not_found_ok and "not found" in message.lower():
            return None
        raise OpsError(message or "Paystack could not complete this request.")
    return payload.get("data") if payload.get("data") is not None else {}


def verify(reference):
    """The Paystack transaction, or None if Paystack has no such reference."""
    return paystack_call("GET", f"/transaction/verify/{urllib.parse.quote(reference, safe='')}", not_found_ok=True)


def paystack_mode():
    key = payments.paystack_secret_key()
    if not key:
        return "off"
    return "live" if key.startswith("sk_live_") else "test"


def _who(user):
    if not user or not getattr(user, "is_authenticated", False):
        return ""
    return user.get_full_name() or user.first_name or user.username


def _naira(kobo):
    return (Decimal(int(kobo or 0)) / 100).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

REFUND_REASONS = {
    "duplicate": "Paid twice",
    "clinic_full": "Paid after the clinic filled",
    "underpaid": "Paid less than the fee",
    "reschedule_failed": "Reschedule couldn't be applied",
    "other": "Other",
}


def refund_payload(r):
    return {
        "id": r.id,
        "reference": r.reference,
        "ref_code": r.ref_code,
        "patient_name": r.patient_name,
        "amount": float(r.amount),
        "reason": r.reason,
        "reason_label": REFUND_REASONS.get(r.reason, r.reason),
        "status": r.status,
        "method": r.method,
        "paystack_refund_id": r.paystack_refund_id,
        "note": r.note,
        "last_error": r.last_error,
        "created_by": r.created_by,
        "processed_by": r.processed_by,
        "created_at": timezone.localtime(r.created_at).isoformat() if r.created_at else None,
        "processed_at": timezone.localtime(r.processed_at).isoformat() if r.processed_at else None,
    }


def _refunded_kobo(reference, exclude_id=None):
    qs = Refund.objects.filter(reference=reference, status__in=[Refund.STATUS_PENDING, Refund.STATUS_PROCESSED])
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    total = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
    return int(Decimal(total) * 100)


def _paystack_refunded_kobo(tx):
    """What Paystack itself has refunded (or is refunding) on this transaction; 0 if it can't say."""
    try:
        rows = paystack_call("GET", "/refund?" + urllib.parse.urlencode({"transaction": tx.get("id"), "perPage": 100}))
    except OpsError:
        return 0
    total = 0
    for r in rows if isinstance(rows, list) else []:
        if str(r.get("status") or "").lower() not in ("failed", "reversed"):
            try:
                total += int(r.get("amount") or 0)
            except (TypeError, ValueError):
                pass
    return total


def _check_refundable(refund, tx):
    if tx is None or str(tx.get("status") or "").lower() != "success":
        raise OpsError("Paystack doesn't show a successful payment for this reference, so there is nothing to refund.")
    paid = int(tx.get("amount") or 0)
    # Refunds recorded here, or made straight on the Paystack dashboard: whichever is more.
    already = max(_refunded_kobo(refund.reference, exclude_id=refund.id), _paystack_refunded_kobo(tx))
    want = int(Decimal(refund.amount) * 100)
    if want <= 0:
        raise OpsError("The refund amount must be more than zero.")
    if already + want > paid:
        left = max(paid - already, 0)
        raise OpsError(f"That would refund more than the patient paid. At most ₦{left / 100:,.2f} can still be refunded "
                       "for this payment.")


def _finish_refund(refund):
    """After a refund is paid back: settle the booking and tell the patient."""
    booking = Booking.objects.filter(ref_code=refund.ref_code).first() if refund.ref_code else None
    if booking and refund.reason == "clinic_full" and booking.payment_status == payments.PAYMENT_STATUS_REFUND:
        Booking.objects.filter(ref_code=booking.ref_code, payment_status=payments.PAYMENT_STATUS_REFUND).update(
            payment_status=payments.PAYMENT_STATUS_REFUNDED,
            delete_reason=f"Refunded {refund.amount} ({refund.method or 'paid back'}) on {timezone.localdate():%d %b %Y}.",
            updated_at=timezone.now(),
        )
    if booking:
        ledger.notify_patient(booking.ref_code, "refunded", amount=refund.amount)


def create_refund(reference, amount, reason, note, user):
    """Staff open a refund that wasn't created automatically (administrators)."""
    reference = str(reference or "").strip()
    if not reference:
        raise OpsError("Enter the Paystack reference of the payment to refund.")
    if not str(note or "").strip():
        raise OpsError("Say why this refund is owed.")
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception:
        raise OpsError("Enter the refund amount in naira, e.g. 15000.")
    tx = verify(reference)
    ref_code = payments.booking_ref_for(reference, tx) or ""
    booking = Booking.objects.filter(ref_code=ref_code).first() if ref_code else None
    refund = Refund(reference=reference, ref_code=ref_code, patient_name=booking.patient_name if booking else "",
                    amount=amount, reason=reason if reason in REFUND_REASONS else "other", note=str(note)[:500],
                    created_by=_who(user))
    _check_refundable(refund, tx)
    refund.save()
    return refund


def refund_with_paystack(refund_id, user, ip=""):
    """Send the refund through Paystack (back to the card or account the patient paid with)."""
    with transaction.atomic():
        claimed = Refund.objects.filter(id=refund_id, status__in=[Refund.STATUS_DUE, Refund.STATUS_FAILED]).update(
            status=Refund.STATUS_PENDING, method="paystack", processed_by=_who(user)[:200], last_error="")
    if not claimed:
        raise OpsError("This refund is already being processed or is closed.")
    refund = Refund.objects.get(id=refund_id)
    try:
        tx = verify(refund.reference)
        _check_refundable(refund, tx)
        data = paystack_call("POST", "/refund", {
            "transaction": refund.reference,
            "amount": int(Decimal(refund.amount) * 100),
            "merchant_note": f"Isalu booking {refund.ref_code}: {REFUND_REASONS.get(refund.reason, 'refund')}"[:100],
        })
    except OpsError as exc:
        Refund.objects.filter(id=refund.id).update(status=Refund.STATUS_FAILED, last_error=str(exc)[:500])
        ledger.record("refund_failed", reference=refund.reference, ref_code=refund.ref_code, patient_name=refund.patient_name,
                      amount=refund.amount, source="staff", actor=_who(user), ip=ip, note=str(exc))
        raise
    status = str((data or {}).get("status") or "pending").lower()
    refund.paystack_refund_id = str((data or {}).get("id") or "")[:40]
    if status == "processed":
        refund.status = Refund.STATUS_PROCESSED
        refund.processed_at = timezone.now()
    refund.save()
    ledger.record("refund_requested", reference=refund.reference, ref_code=refund.ref_code, patient_name=refund.patient_name,
                  amount=refund.amount, source="staff", actor=_who(user), ip=ip,
                  note=f"Sent to Paystack (refund {refund.paystack_refund_id or 'id pending'}).")
    if refund.status == Refund.STATUS_PROCESSED:
        ledger.record("refund_processed", reference=refund.reference, ref_code=refund.ref_code,
                      patient_name=refund.patient_name, amount=refund.amount, source="staff", actor=_who(user), ip=ip)
        _finish_refund(refund)
    return refund


def record_manual_refund(refund_id, method, note, user, ip=""):
    """The money was paid back outside Paystack (bank transfer or cash)."""
    if method not in ("bank_transfer", "cash"):
        raise OpsError("Choose how the money was paid back: bank transfer or cash.")
    if not str(note or "").strip():
        raise OpsError("Add the transfer reference or receipt number, so the refund can be traced.")
    with transaction.atomic():
        claimed = Refund.objects.filter(id=refund_id, status__in=[Refund.STATUS_DUE, Refund.STATUS_FAILED]).update(
            status=Refund.STATUS_PROCESSED, method=method, processed_by=_who(user)[:200],
            processed_at=timezone.now(), note=str(note)[:500], last_error="")
    if not claimed:
        raise OpsError("This refund is already being processed or is closed.")
    refund = Refund.objects.get(id=refund_id)
    ledger.record("refund_processed", reference=refund.reference, ref_code=refund.ref_code, patient_name=refund.patient_name,
                  amount=refund.amount, source="staff", actor=_who(user), ip=ip,
                  note=f"Paid back by {method.replace('_', ' ')}: {note}")
    _finish_refund(refund)
    return refund


def cancel_refund(refund_id, note, user, ip=""):
    """Not owed after all (e.g. the patient took another date instead)."""
    if not str(note or "").strip():
        raise OpsError("Say why the refund is no longer owed.")
    claimed = Refund.objects.filter(id=refund_id, status__in=[Refund.STATUS_DUE, Refund.STATUS_FAILED]).update(
        status=Refund.STATUS_CANCELLED, processed_by=_who(user)[:200], processed_at=timezone.now(), note=str(note)[:500])
    if not claimed:
        raise OpsError("Only a refund that hasn't been sent can be cancelled.")
    refund = Refund.objects.get(id=refund_id)
    ledger.record("refund_failed", reference=refund.reference, ref_code=refund.ref_code, patient_name=refund.patient_name,
                  amount=refund.amount, source="staff", actor=_who(user), ip=ip, note=f"Refund cancelled: {note}")
    return refund


def apply_refund_webhook(event, data):
    """refund.pending / refund.processing / refund.processed / refund.failed from Paystack."""
    data = data or {}
    refund_id = str(data.get("id") or data.get("refund_reference") or "")
    tx_ref = str(data.get("transaction_reference") or (data.get("transaction") or {}).get("reference") or "")
    refund = None
    if refund_id:
        refund = Refund.objects.filter(paystack_refund_id=refund_id).first()
    if refund is None and tx_ref:
        refund = Refund.objects.filter(reference=tx_ref, status=Refund.STATUS_PENDING, method="paystack").first()
    if refund is None:
        return "no matching refund"
    if event == "refund.processed":
        if refund.status == Refund.STATUS_PROCESSED:
            return "already processed"
        Refund.objects.filter(id=refund.id).update(status=Refund.STATUS_PROCESSED, processed_at=timezone.now())
        refund.refresh_from_db()
        ledger.record("refund_processed", reference=refund.reference, ref_code=refund.ref_code,
                      patient_name=refund.patient_name, amount=refund.amount, source="webhook",
                      note=f"Paystack refund {refund.paystack_refund_id} processed.")
        _finish_refund(refund)
        return "refund processed"
    if event == "refund.failed":
        Refund.objects.filter(id=refund.id).update(status=Refund.STATUS_FAILED,
                                                   last_error="Paystack couldn't complete the refund. Try again or pay back manually.")
        ledger.record("refund_failed", reference=refund.reference, ref_code=refund.ref_code,
                      patient_name=refund.patient_name, amount=refund.amount, source="webhook",
                      note="Paystack couldn't complete the refund.")
        return "refund failed"
    return "refund in progress"


# ---------------------------------------------------------------------------
# Disputes and reversals
# ---------------------------------------------------------------------------

def _booking_for(reference, tx=None):
    code = payments.booking_ref_for(reference, tx)
    return Booking.objects.filter(ref_code=code).first() if code else None


def mark_reversed(reference, tx=None, source="sweep", actor="", ip=""):
    """Paystack reversed a payment the booking counts as paid: un-clear it."""
    booking = _booking_for(reference, tx)
    if booking is None or booking.payment_method != ONLINE or (booking.invoice_ref or booking.payment_reference) != reference:
        return None
    if booking.payment_status not in (payments.PAYMENT_STATUS_PAID, payments.PAYMENT_STATUS_DISPUTED):
        return None
    Booking.objects.filter(ref_code=booking.ref_code).update(
        payment_status=payments.PAYMENT_STATUS_REVERSED,
        delete_reason="Paystack reversed the online payment. Collect payment at the Cashdesk before the patient is seen.",
        updated_at=timezone.now(),
    )
    ledger.record("payment_reversed", reference=reference, booking=booking, amount=booking.amount_paid, source=source,
                  actor=actor, ip=ip, once=True, note="Paystack shows this payment as reversed; booking no longer cleared.")
    return booking


def apply_dispute_webhook(event, data):
    data = data or {}
    tx = data.get("transaction") or {}
    reference = str(tx.get("reference") or data.get("transaction_reference") or "")
    if not reference:
        return "no reference"
    booking = _booking_for(reference, tx)
    dispute_id = str(data.get("id") or "")
    if event in ("charge.dispute.create", "charge.dispute.remind"):
        if booking and booking.payment_status == payments.PAYMENT_STATUS_PAID and booking.payment_method == ONLINE:
            Booking.objects.filter(ref_code=booking.ref_code).update(
                payment_status=payments.PAYMENT_STATUS_DISPUTED,
                delete_reason="The patient's bank opened a dispute on this online payment. Respond on the Paystack "
                              "dashboard; collect payment at the Cashdesk if the patient arrives before it is resolved.",
                updated_at=timezone.now(),
            )
        ledger.record("dispute_opened", reference=reference, booking=booking, amount=_naira(tx.get("amount")) if tx.get("amount") else None,
                      source="webhook", once=True, note=f"Dispute {dispute_id} opened: {data.get('reason') or data.get('category') or 'no reason given'}.",
                      data={"dispute_id": dispute_id, "due_at": data.get("dueAt") or data.get("due_at")})
        return "dispute opened"
    if event == "charge.dispute.resolve":
        resolution = str(data.get("resolution") or data.get("status") or "").lower()
        patient_won = resolution in ("merchant-accepted", "lost", "resolved-merchant-accepted")
        if booking and booking.payment_status == payments.PAYMENT_STATUS_DISPUTED:
            if patient_won:
                Booking.objects.filter(ref_code=booking.ref_code).update(
                    payment_status=payments.PAYMENT_STATUS_REVERSED,
                    delete_reason="Dispute resolved in the patient's favour; the payment was returned to them.",
                    updated_at=timezone.now())
            else:
                Booking.objects.filter(ref_code=booking.ref_code).update(
                    payment_status=payments.PAYMENT_STATUS_PAID, delete_reason="", updated_at=timezone.now())
        ledger.record("dispute_lost" if patient_won else "dispute_won", reference=reference, booking=booking,
                      source="webhook", note=f"Dispute {dispute_id} resolved: {resolution or 'unknown'}.")
        return "dispute resolved"
    return "ignored"


# ---------------------------------------------------------------------------
# Automatic check (sweep)
# ---------------------------------------------------------------------------

SWEEP_KEY = "payment_sweep"
MAX_CALLS = 150


def sweep_minutes():
    try:
        return max(0, int(getattr(settings, "PAYMENT_SWEEP_MINUTES", 15)))
    except (TypeError, ValueError):
        return 15


def _claim_lock(minutes=10):
    now = timezone.now()
    PaymentJobState.objects.get_or_create(key=SWEEP_KEY)
    return PaymentJobState.objects.filter(key=SWEEP_KEY).filter(
        Q(locked_until__isnull=True) | Q(locked_until__lt=now)
    ).update(locked_until=now + datetime.timedelta(minutes=minutes)) == 1


def sweep_state():
    state = PaymentJobState.objects.filter(key=SWEEP_KEY).first()
    if not state or not state.last_run_at:
        return {"last_run_at": None, "result": {}, "every_minutes": sweep_minutes()}
    return {"last_run_at": timezone.localtime(state.last_run_at).isoformat(), "result": state.last_result or {},
            "every_minutes": sweep_minutes()}


def run_sweep(force=False):
    """
    Check unconfirmed and recently confirmed online payments with Paystack.
    Safe to run from several processes: one run at a time.
    """
    if not payments.paystack_enabled():
        return {"skipped": "online payment is off"}
    if not _claim_lock():
        if not force:
            return {"skipped": "another check is running"}
        return {"skipped": "another check is running; try again in a few minutes"}

    now = timezone.now()
    summary = defaultdict(int)
    calls = 0
    errors = []
    try:
        payments.expire_payment_holds()

        # 1. Attempts that might have been paid: not success/failed/reversed yet, from the last 3 days,
        #    checked no more than every 10 minutes.
        open_attempts = (
            PaymentAttempt.objects.filter(created_at__gte=now - datetime.timedelta(days=3))
            .exclude(status__in=["success", "failed", "reversed"])
            .filter(Q(last_checked_at__isnull=True) | Q(last_checked_at__lt=now - datetime.timedelta(minutes=10)))
            .order_by("-created_at")
        )
        for attempt in open_attempts[:MAX_CALLS]:
            if calls >= MAX_CALLS:
                break
            calls += 1
            summary["attempts_checked"] += 1
            try:
                tx = verify(attempt.reference)
            except OpsError as exc:
                errors.append(str(exc))
                break
            if tx is None:
                PaymentAttempt.objects.filter(pk=attempt.pk).update(status="not_found", last_checked_at=now)
                continue
            if str(tx.get("status") or "").lower() == "success":
                booking, outcome = payments.confirm_payment(attempt.reference, tx, source="sweep")
                summary[f"outcome_{outcome}"] += 1
                if outcome == "paid" and booking is not None:
                    _broadcast(booking, f"Online payment confirmed automatically for {booking.patient_name} ({booking.ref_code}).")
            else:
                payments._note_attempt(attempt.reference, attempt.booking_ref, tx)

        # 2. Paid online in the last 3 days: still paid on Paystack? (each at most every 6 hours)
        recent = (
            Booking.objects.filter(payment_method=ONLINE, paid_at__gte=now - datetime.timedelta(days=3),
                                   payment_status__in=[payments.PAYMENT_STATUS_PAID, payments.PAYMENT_STATUS_DISPUTED])
            .exclude(invoice_ref="")
        )
        for b in recent:
            if calls >= MAX_CALLS:
                break
            attempt = PaymentAttempt.objects.filter(reference=b.invoice_ref).first()
            if attempt and attempt.last_checked_at and attempt.last_checked_at > now - datetime.timedelta(hours=6):
                continue
            calls += 1
            summary["paid_rechecked"] += 1
            try:
                tx = verify(b.invoice_ref)
            except OpsError as exc:
                errors.append(str(exc))
                break
            status = str((tx or {}).get("status") or "").lower()
            if tx is not None:
                payments._note_attempt(b.invoice_ref, b.ref_code, tx)
            if status == "reversed":
                if mark_reversed(b.invoice_ref, tx, source="sweep"):
                    summary["reversed"] += 1
            elif status != "success":
                if ledger.record("cleared_without_payment", reference=b.invoice_ref, booking=b, source="sweep", once=True,
                                 note=f"Booking is cleared but Paystack shows '{status or 'no transaction'}'."):
                    summary["cleared_without_payment"] += 1

        # 3. Successful booking payments on Paystack in the last 2 days that this system hasn't seen.
        if calls < MAX_CALLS:
            try:
                fmt = "%Y-%m-%dT%H:%M:%S.000Z"
                rows = paystack_call("GET", "/transaction?" + urllib.parse.urlencode({
                    "status": "success", "perPage": 100,
                    "from": (now - datetime.timedelta(days=2)).astimezone(datetime.timezone.utc).strftime(fmt),
                    "to": now.astimezone(datetime.timezone.utc).strftime(fmt),
                })) or []
                calls += 1
                known_paid = set(Booking.objects.filter(payment_method=ONLINE).exclude(invoice_ref="")
                                 .values_list("invoice_ref", flat=True))
                # Payments already handled once (confirmed, flagged as duplicate/short/no booking) are skipped.
                known_paid |= set(PaymentAttempt.objects.filter(status="success").values_list("reference", flat=True))
                for tx in rows if isinstance(rows, list) else []:
                    ref = str(tx.get("reference") or "")
                    if not ref or ref in known_paid or str(tx.get("status") or "").lower() != "success":
                        continue
                    if not (ref.upper().startswith("ISALU-") or payments.tx_ref_code(tx)):
                        continue   # not a booking payment
                    summary["paystack_list_unseen"] += 1
                    booking, outcome = payments.confirm_payment(ref, tx, source="sweep")
                    summary[f"outcome_{outcome}"] += 1
                    if outcome == "paid" and booking is not None:
                        _broadcast(booking, f"Online payment confirmed automatically for {booking.patient_name} ({booking.ref_code}).")
            except OpsError as exc:
                errors.append(str(exc))
    finally:
        result = {**dict(summary), "paystack_calls": calls, "errors": errors[:3], "mode": paystack_mode()}
        PaymentJobState.objects.filter(key=SWEEP_KEY).update(last_run_at=timezone.now(), last_result=result, locked_until=None)
    return result


def _broadcast(booking, message):
    try:
        from .views import broadcast_booking_update
        broadcast_booking_update(booking, event_type="BOOKING_UPDATE", message=message)
    except Exception:
        logger.exception("broadcast failed for %s", booking.ref_code)


# ---------------------------------------------------------------------------
# Daily reconciliation
# ---------------------------------------------------------------------------

def _local_day(value):
    if not value:
        return None
    dt = value if isinstance(value, datetime.datetime) else parse_datetime(str(value))
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)
    return timezone.localtime(dt).date()


def daily(day):
    """Paystack's booking money for one day (Lagos time) against the bookings and refunds."""
    from .payment_report import ReportError, _day_bounds_utc, _get_all, is_booking_transaction

    if not payments.paystack_enabled():
        raise OpsError("Online payment is switched off, so there is nothing to reconcile.")
    utc_from, utc_to = _day_bounds_utc(day, day)
    try:
        txs, truncated = _get_all("/transaction", {"from": utc_from, "to": utc_to})
    except ReportError as exc:
        raise OpsError(str(exc))
    # Paid that day, including payments Paystack later reversed (shown, but not counted as money in).
    txs = [t for t in txs if str(t.get("status") or "").lower() in ("success", "reversed") and is_booking_transaction(t)
           and _local_day(t.get("paid_at") or t.get("paidAt") or t.get("created_at")) == day]

    refs = [str(t.get("reference")) for t in txs]
    codes = {payments.booking_ref_for(r, t) for r, t in zip(refs, txs)} - {None}
    bookings = {b.ref_code: b for b in Booking.objects.filter(ref_code__in=codes)}
    refunds = defaultdict(list)
    for r in Refund.objects.filter(reference__in=refs).exclude(status=Refund.STATUS_CANCELLED):
        refunds[r.reference].append(r)

    from .models import ReschedulePayment
    reschedules = {r.reference: r for r in ReschedulePayment.objects.filter(reference__in=refs)}
    rows = []
    totals = defaultdict(Decimal)
    for t, ref in zip(txs, refs):
        amount, fees = _naira(t.get("amount")), _naira(t.get("fees"))
        code = payments.booking_ref_for(ref, t)
        b = bookings.get(code) if code else None
        paid_ref = (b.invoice_ref or "") if b and b.payment_method == ONLINE else ""
        rs = reschedules.get(ref)
        if str(t.get("status") or "").lower() == "reversed":
            state = "reversed"
        elif rs is not None:
            state = {"applied": "reschedule", "not_applied": "refund", "failed": "short"}.get(rs.status, "unconfirmed")
        elif b is None:
            state = "no_booking"
        elif b.payment_status == payments.PAYMENT_STATUS_PAID and paid_ref == ref:
            state = "matched"
        elif b.payment_status == payments.PAYMENT_STATUS_DISPUTED and paid_ref == ref:
            state = "disputed"
        elif b.payment_status == payments.PAYMENT_STATUS_REVERSED and paid_ref == ref:
            state = "reversed"
        elif b.payment_status in (payments.PAYMENT_STATUS_REFUND, payments.PAYMENT_STATUS_REFUNDED) and paid_ref == ref:
            state = "refund"
        elif b.payment_status in (payments.PAYMENT_STATUS_PAID, payments.PAYMENT_STATUS_DISPUTED,
                                  payments.PAYMENT_STATUS_REFUND, payments.PAYMENT_STATUS_REFUNDED):
            state = "duplicate"
        else:
            state = "unconfirmed"
        fee = Decimal(b.fee_amount or 0) if b else Decimal("0")
        if state == "matched" and amount < fee:
            state = "short"
        refunded = sum((Decimal(r.amount) for r in refunds[ref] if r.status == Refund.STATUS_PROCESSED), Decimal("0"))
        owed = sum((Decimal(r.amount) for r in refunds[ref] if r.status in Refund.OPEN_STATUSES), Decimal("0"))
        rows.append({
            "reference": ref,
            "paid_at": timezone.localtime(parse_datetime(str(t.get("paid_at") or t.get("paidAt")))).isoformat()
            if (t.get("paid_at") or t.get("paidAt")) else None,
            "amount": float(amount), "fees": float(fees), "net": float(amount - fees),
            "clinic_fee": float(fee) if b else None,
            "channel": t.get("channel") or "",
            "state": state,
            "ref_code": b.ref_code if b else (code or ""),
            "patient_name": b.patient_name if b else "",
            "clinic": b.doctor_specialty if b else "",
            "booking_status": b.status if b else "",
            "payment_status": b.payment_status if b else "",
            "refunded": float(refunded), "refund_owed": float(owed),
        })
        if state == "reversed" and str(t.get("status") or "").lower() == "reversed":
            totals["reversed"] += amount
            continue
        totals["gross"] += amount
        totals["fees"] += fees
        totals["net"] += amount - fees
        totals["refunded"] += refunded
        totals["refund_owed"] += owed
        if state == "reschedule":
            totals["reschedule_fees"] += amount
        if state == "matched":
            totals["confirmed_fees"] += fee
            totals["customer_charges"] += amount - fee

    # Bookings this system shows as paid online today that Paystack doesn't list as paid today.
    start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))
    end = start + datetime.timedelta(days=1)
    local_only = []
    for b in Booking.objects.filter(payment_method=ONLINE, paid_at__gte=start, paid_at__lt=end).exclude(invoice_ref__in=refs):
        local_only.append({"ref_code": b.ref_code, "patient_name": b.patient_name, "reference": b.invoice_ref,
                           "amount": float(b.amount_paid or 0), "payment_status": b.payment_status,
                           "note": "Recorded as paid today, but Paystack lists no successful payment today with this reference."})

    refunds_today = Refund.objects.filter(status=Refund.STATUS_PROCESSED, processed_at__gte=start, processed_at__lt=end)
    refunds_today_total = refunds_today.aggregate(t=Sum("amount"))["t"] or Decimal("0")

    counts = defaultdict(int)
    for r in rows:
        counts[r["state"]] += 1
    issues = sum(v for k, v in counts.items() if k not in ("matched", "reschedule")) + len(local_only)
    return {
        "date": day.isoformat(),
        "mode": paystack_mode(),
        "truncated": truncated,
        "totals": {k: float(v) for k, v in totals.items()} | {"payments": sum(1 for r in rows if r["state"] != "reversed"),
                                                             "refunds_paid_today": float(refunds_today_total),
                                                             "net_after_refunds_today": float(totals["net"] - refunds_today_total)},
        "counts": dict(counts),
        "issues": issues,
        "balanced": issues == 0,
        "rows": sorted(rows, key=lambda r: (r["state"] in ("matched", "reschedule"), r["paid_at"] or "")),
        "local_only": local_only,
        "refunds_today": [refund_payload(r) for r in refunds_today],
    }
