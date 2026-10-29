"""
Money ledger helpers shared by the payment flow, the webhook, the automatic
check and the Payment Reconciliation module.

- record(): append a PaymentEvent (never edited or deleted).
- open_refund(): create a refund that is owed (once per payment and reason).
- notify_patient(): send the patient a receipt / refund notice, once, in the
  background, after the database change is committed.
"""

import logging
import threading
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Booking, PaymentEvent, Refund

logger = logging.getLogger(__name__)


def _dec(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return None


def record(kind, *, reference="", booking=None, ref_code="", patient_name="", amount=None,
           source="system", actor="", ip="", note="", data=None, once=False):
    """Append a ledger entry. once=True skips it if this kind is already recorded for this reference."""
    ref_code = ref_code or (booking.ref_code if booking else "")
    patient_name = patient_name or (booking.patient_name if booking else "")
    if once and reference and PaymentEvent.objects.filter(kind=kind, reference=reference).exists():
        return None
    try:
        return PaymentEvent.objects.create(
            kind=kind,
            reference=str(reference or "")[:100],
            ref_code=str(ref_code or "")[:20],
            patient_name=str(patient_name or "")[:200],
            amount=_dec(amount),
            source=str(source or "system")[:20],
            actor=str(actor or "")[:200],
            ip=str(ip or "")[:60],
            note=str(note or "")[:500],
            data=data or {},
        )
    except Exception:
        logger.exception("Couldn't record payment event %s for %s", kind, reference)
        return None


def open_refund(*, reference, booking=None, amount, reason, note="", created_by="system"):
    """A refund that is owed. One open refund per payment reference and reason."""
    amount = _dec(amount)
    if not reference or not amount or amount <= 0:
        return None
    existing = Refund.objects.filter(reference=reference, reason=reason).exclude(status=Refund.STATUS_CANCELLED).first()
    if existing:
        return existing
    return Refund.objects.create(
        reference=reference,
        ref_code=booking.ref_code if booking else "",
        patient_name=booking.patient_name if booking else "",
        amount=amount,
        reason=reason,
        status=Refund.STATUS_DUE,
        note=str(note or "")[:500],
        created_by=str(created_by or "system")[:200],
    )


def notify_patient(ref_code, kind, **extra):
    """
    kind: "paid" (receipt), "refund_due" (paid but the clinic filled),
    "refunded" (money sent back). Runs after commit, in a background thread,
    so Paystack's webhook and the staff screen never wait for SMS/email.
    """
    def send():
        try:
            from .notification_service import send_payment_notice

            booking = Booking.objects.filter(ref_code=ref_code).first()
            if booking is None:
                return
            if kind == "paid":
                # Claim the receipt so two paths (browser + webhook) never both send it.
                claimed = Booking.objects.filter(ref_code=ref_code, payment_receipt_sent_at__isnull=True).update(
                    payment_receipt_sent_at=timezone.now()
                )
                if not claimed:
                    return
            result = send_payment_notice(booking, kind, **extra)
            record("receipt_sent", reference=booking.payment_reference, booking=booking, source="system",
                   note=f"{kind}: " + ("sent" if result.get("delivered") else "not delivered") +
                   f" (SMS: {result.get('sms_message')}; email: {result.get('email_message')})")
        except Exception:
            logger.exception("Couldn't notify %s about %s", ref_code, kind)
        finally:
            from django.db import connection
            connection.close()   # this thread's own database connection

    def start():
        threading.Thread(target=send, daemon=True).start()

    transaction.on_commit(start)
