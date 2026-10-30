"""
Public endpoints for paying private self-pay bookings online with Paystack.

    GET  /api/payments/config/            what the booking page needs to know
    POST /api/payments/start/             hold the slot and open a Paystack checkout
    GET  /api/payments/verify/?reference= confirm after Paystack sends the patient back
    POST /api/payments/paystack-webhook/  Paystack's server-to-server confirmation
"""

import datetime
import json
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from . import payments
from .models import Booking, Doctor
from .scheduling import AWAITING_PAYMENT
from .serializers import BookingSerializer
from .views import PublicBookingThrottle, PublicLookupThrottle, broadcast_booking_update, flatten_error_detail, public_booking_payload

logger = logging.getLogger(__name__)


def client_ip(request):
    """The caller's address. Behind a proxy set TRUST_X_FORWARDED_FOR=True so the real address is used."""
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return str(request.META.get("REMOTE_ADDR") or "")


def _callback_url(request):
    """Where Paystack returns the patient: <PUBLIC_SITE_URL>/book."""
    base = getattr(settings, "PUBLIC_SITE_URL", "")
    if not base:
        # Fall back to the page that asked, but only for trusted origins.
        origin = str(request.data.get("return_origin") or request.headers.get("Origin") or "").rstrip("/")
        allowed = set(getattr(settings, "CORS_ALLOWED_ORIGINS", []) or [])
        host = urlparse(origin).hostname or ""
        if origin and (origin in allowed or settings.DEBUG or host in ("localhost", "127.0.0.1")):
            base = origin
    if not base:
        raise payments.PaymentError(
            "Online payment isn't fully set up yet (the booking site address is missing). "
            "Please book with the hospital directly."
        )
    return f"{base}/book?payment=return"


class PaymentConfigView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({
            "online_payments_enabled": payments.paystack_enabled(),
            "private_bookings_require_payment": payments.payment_mandatory(),
            "hold_minutes": payments.hold_minutes(),
            "hmo_min_notice_hours": payments.hmo_notice_hours(),
            "hmo_close_minutes": payments.HMO_CLOSE_MINUTES,
            "currency": "NGN",
        })


class StartPaymentView(APIView):
    """
    Body: the same booking details the booking form sends today
    (doctor_id, date, time, patient_name, patient_phone, patient_email, ...).
    Or {"ref_code": "..."} to retry payment for a booking that is still held.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicBookingThrottle]

    def post(self, request):
        if not payments.paystack_enabled():
            return Response({"error": "Online payment is switched off. You can pay at the hospital cashdesk."},
                            status=status.HTTP_400_BAD_REQUEST)
        payments.expire_payment_holds()
        try:
            callback = _callback_url(request)
            retry_ref = str(request.data.get("ref_code") or "").strip()
            if retry_ref:
                booking = Booking.objects.filter(ref_code=retry_ref).first()
                if booking is None:
                    return Response({"error": "We couldn't find that booking."}, status=status.HTTP_404_NOT_FOUND)
            else:
                booking = self._hold_booking(request)
                if isinstance(booking, Response):
                    return booking
            checkout = payments.open_transaction(booking, callback)
        except payments.PaymentError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            **checkout,
            "ref_code": booking.ref_code,
            "amount": float(booking.fee_amount),
            "currency": "NGN",
            "expires_at": booking.payment_expires_at.isoformat() if booking.payment_expires_at else None,
            "hold_minutes": payments.hold_minutes(),
        }, status=status.HTTP_201_CREATED)

    def _hold_booking(self, request):
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        data["payment_type"] = "Private Self-Pay"
        data["paymentType"] = "Private Self-Pay"
        serializer = BookingSerializer(data=data, context={"request": request})
        if not serializer.is_valid():
            return Response(flatten_error_detail(serializer.errors), status=status.HTTP_400_BAD_REQUEST)

        doctor = Doctor.objects.select_related("department").filter(doc_id=serializer.validated_data["doctor_id"]).first()
        fee = payments.clinic_fee(doctor, serializer.validated_data.get("visit_type"))
        if fee <= 0:
            return Response(
                {"error": "This clinic has no online consultation fee. Book without paying online and pay at the cashdesk."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            booking = serializer.save(
                status=AWAITING_PAYMENT,
                is_active=True,
                payment_type="Private Self-Pay",
                payment_status=payments.PAYMENT_STATUS_AWAITING,
                payment_method=payments.ONLINE_METHOD,
                hmo_name="N/A",
                hmo_policy_code="",
                hmo_status="N/A",
                hmo_auth_code="",
                invoice_ref="",
                delete_reason="",
                reminder_sent=False,
                fee_amount=fee,
                payment_expires_at=timezone.now() + datetime.timedelta(minutes=payments.hold_minutes()),
            )
            # The patient changed their mind about the date: free their older unpaid hold(s).
            from .duplicates import release_other_payment_holds
            release_other_payment_holds(doctor, booking.patient_name, booking.patient_phone, booking.ref_code)
        return booking


class VerifyPaymentView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicLookupThrottle]

    def get(self, request):
        reference = str(request.query_params.get("reference") or request.query_params.get("trxref") or "").strip()
        if not reference:
            return Response({"error": "Missing payment reference."}, status=status.HTTP_400_BAD_REQUEST)
        if not payments.paystack_enabled():
            return Response({"error": "Online payment is switched off."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            booking, outcome = payments.confirm_payment(reference, source="verify", ip=client_ip(request))
        except payments.PaymentError as exc:
            return Response({"error": str(exc), "outcome": "unreachable"}, status=status.HTTP_502_BAD_GATEWAY)

        if booking is None:
            return Response({"error": "We couldn't find a booking for this payment.", "outcome": outcome},
                            status=status.HTTP_404_NOT_FOUND)
        if outcome == "paid":
            broadcast_booking_update(booking, event_type="NEW_BOOKING",
                                     message=f"New paid appointment for {booking.patient_name} ({booking.ref_code}).")
        payments.expire_payment_holds()
        booking.refresh_from_db()
        # What the booking page understands: a second payment still shows the ticket
        # (staff refund the extra one); a short payment is not a confirmed booking.
        shown = {"duplicate": "already_paid", "underpaid": "failed"}.get(outcome, outcome)
        return Response({"outcome": shown, "booking": public_booking_payload(booking)})


class PaystackWebhookView(APIView):
    """
    Paystack's server-to-server notifications, signed with the secret key.

    - Signature checked (HMAC-SHA512) before anything else; optional IP
      allowlist (PAYSTACK_WEBHOOK_IPS in .env).
    - Each notification is handled once: a replay of the same signed body is
      acknowledged and ignored.
    - charge.success is re-checked with Paystack, never trusted from the body.
    - Refund and dispute notifications update refunds and bookings.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []
    MAX_BODY = 512 * 1024

    def post(self, request):
        import hashlib
        from django.db import IntegrityError

        from . import payment_ops
        from .models import WebhookEvent

        allowed_ips = getattr(settings, "PAYSTACK_WEBHOOK_IPS", [])
        if allowed_ips and client_ip(request) not in allowed_ips:
            logger.warning("Paystack webhook from unexpected address %s refused", client_ip(request))
            return Response(status=status.HTTP_403_FORBIDDEN)
        raw = request.body
        if len(raw) > self.MAX_BODY:
            return Response(status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        if not payments.valid_webhook_signature(raw, request.headers.get("X-Paystack-Signature", "")):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        try:
            event = json.loads(raw.decode() or "{}")
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)
        kind = str(event.get("event") or "")
        data = event.get("data") or {}
        reference = str(data.get("reference") or data.get("transaction_reference")
                        or (data.get("transaction") or {}).get("reference") or "")[:100]

        try:
            record = WebhookEvent.objects.create(key=hashlib.sha256(raw).hexdigest(), event=kind[:60], reference=reference)
        except IntegrityError:
            return Response({"received": True, "duplicate": True})

        try:
            if kind == "charge.success":
                # Re-check with Paystack rather than trusting the payload alone.
                booking, outcome = payments.confirm_payment(reference, source="webhook", ip=client_ip(request))
                if booking is not None and outcome == "paid":
                    broadcast_booking_update(booking, event_type="NEW_BOOKING",
                                             message=f"New paid appointment for {booking.patient_name} ({booking.ref_code}).")
                result = outcome
            elif kind.startswith("refund."):
                result = payment_ops.apply_refund_webhook(kind, data)
            elif kind.startswith("charge.dispute."):
                result = payment_ops.apply_dispute_webhook(kind, data)
            else:
                result = "ignored"
        except (payments.PaymentError, payment_ops.OpsError):
            logger.exception("Webhook %s for %s failed", kind, reference)
            record.delete()   # let Paystack's retry be processed
            return Response(status=status.HTTP_502_BAD_GATEWAY)
        WebhookEvent.objects.filter(pk=record.pk).update(result=str(result)[:200])
        return Response({"received": True})


class PaymentReportView(APIView):
    """
    GET /api/payments/report/?from=YYYY-MM-DD&to=YYYY-MM-DD[&refresh=1]
    Booking payments and payouts read live from Paystack (staff with the
    Booking Payments module; administrators always).
    """

    def get(self, request):
        from .payment_report import ReportError, build_report, parse_range
        from .views import is_staff_request, user_has_desk

        if not is_staff_request(request):
            return Response({"error": "Please sign in."}, status=status.HTTP_401_UNAUTHORIZED)
        if not user_has_desk(request.user, "booking_payments"):
            return Response(
                {"error": "Your role doesn't include the Booking Payments module."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            date_from, date_to = parse_range(request.query_params.get("from"), request.query_params.get("to"))
            report = build_report(date_from, date_to, refresh=request.query_params.get("refresh") in ("1", "true"))
        except ReportError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(report)


class ReconcileThrottle(UserRateThrottle):
    """Checks call Paystack: at most PAYMENT_RECONCILE_RATE per staff member (default 60/min)."""
    scope = "payment_reconcile"


class MoneyActionThrottle(UserRateThrottle):
    """Confirmations and refunds: at most PAYMENT_MONEY_RATE per staff member (default 20/min)."""
    scope = "payment_money"


def _reconcile_access(request, need="check"):
    """None if allowed, else the error Response."""
    from . import reconciliation
    from .views import is_staff_request

    if not is_staff_request(request):
        return Response({"error": "Please sign in."}, status=status.HTTP_401_UNAUTHORIZED)
    perms = reconciliation.permissions_for(request.user)
    if not perms.get("check"):
        return Response({"error": "Your role doesn't include the Payment Reconciliation module."},
                        status=status.HTTP_403_FORBIDDEN)
    if not perms.get(need):
        messages = {
            "confirm": "Only Finance, Cashdesk, Booking Payments and administrators can confirm bookings from a payment.",
            "record_refund": "Only Finance, Cashdesk, Booking Payments and administrators can record refunds.",
            "paystack_refund": "Only Finance, Booking Payments staff and administrators can send refunds through Paystack.",
            "create_refund": "Only Finance and administrators can open a new refund by hand.",
            "run_check": "Only Finance, Booking Payments staff and administrators can run the automatic check now.",
            "view_money": "Daily reconciliation, the money ledger and refunds are for Finance and the Cashdesk.",
        }
        return Response({"error": messages.get(need, "You can't do that.")}, status=status.HTTP_403_FORBIDDEN)
    return None


class PaymentReconcileView(APIView):
    """
    Payment Reconciliation.

    GET  /api/payments/reconcile/?q=<Paystack reference or booking reference>   check with Paystack
    GET  /api/payments/reconcile/                                                overview
    POST /api/payments/reconcile/ {"reference": "..."}                           confirm the booking
    """

    def get_throttles(self):
        return [ReconcileThrottle()] if self.request.method == "GET" else [MoneyActionThrottle()]

    def get(self, request):
        from . import reconciliation

        denied = _reconcile_access(request)
        if denied:
            return denied
        query = str(request.query_params.get("q") or request.query_params.get("reference") or "").strip()
        try:
            if query:
                return Response(reconciliation.check(query, request.user))
            return Response(reconciliation.overview(request.user))
        except reconciliation.ReconcileError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request):
        from . import reconciliation

        denied = _reconcile_access(request, "confirm")
        if denied:
            return denied
        try:
            result, outcome, booking = reconciliation.confirm(request.data.get("reference"), request.user, client_ip(request))
        except reconciliation.ReconcileError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if booking is not None and outcome in ("paid", "refund_due"):
            message = (f"Online payment confirmed for {booking.patient_name} ({booking.ref_code})."
                       if outcome == "paid" else
                       f"Online payment for {booking.patient_name} ({booking.ref_code}) needs a refund: clinic full.")
            broadcast_booking_update(booking, event_type="NEW_BOOKING" if outcome == "paid" else "BOOKING_UPDATE", message=message)
        return Response(result)


class ReconcileRefundView(APIView):
    """
    POST /api/payments/reconcile/refunds/
      {"action": "paystack", "id": 12}                                   send through Paystack
      {"action": "manual", "id": 12, "method": "bank_transfer", "note": "..."}   paid back outside Paystack
      {"action": "cancel", "id": 12, "note": "..."}                      not owed after all
      {"action": "create", "reference": "...", "amount": 15000, "reason": "other", "note": "..."}   (administrators)
    """

    throttle_classes = [MoneyActionThrottle]

    def post(self, request):
        from . import payment_ops

        action = str(request.data.get("action") or "")
        need = {"paystack": "paystack_refund", "manual": "record_refund", "cancel": "record_refund",
                "create": "create_refund"}.get(action)
        if not need:
            return Response({"error": "Unknown refund action."}, status=status.HTTP_400_BAD_REQUEST)
        denied = _reconcile_access(request, need)
        if denied:
            return denied
        ip = client_ip(request)
        try:
            refund_id = int(request.data.get("id") or 0)
        except (TypeError, ValueError):
            refund_id = 0
        try:
            if action == "paystack":
                refund = payment_ops.refund_with_paystack(refund_id, request.user, ip)
            elif action == "manual":
                refund = payment_ops.record_manual_refund(refund_id, str(request.data.get("method") or ""),
                                                          str(request.data.get("note") or ""), request.user, ip)
            elif action == "cancel":
                refund = payment_ops.cancel_refund(refund_id, str(request.data.get("note") or ""), request.user, ip)
            else:
                refund = payment_ops.create_refund(request.data.get("reference"), request.data.get("amount"),
                                                   str(request.data.get("reason") or "other"),
                                                   str(request.data.get("note") or ""), request.user)
        except payment_ops.OpsError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # e.g. a refund id that doesn't exist
            if exc.__class__.__name__ == "DoesNotExist":
                return Response({"error": "That refund doesn't exist."}, status=status.HTTP_404_NOT_FOUND)
            raise
        return Response({"refund": payment_ops.refund_payload(refund)})


class ReconcileDailyView(APIView):
    """GET /api/payments/reconcile/daily/?date=YYYY-MM-DD   Paystack's money for a day against the bookings."""

    throttle_classes = [ReconcileThrottle]

    def get(self, request):
        import datetime as _dt

        from . import payment_ops

        denied = _reconcile_access(request, "view_money")
        if denied:
            return denied
        raw = request.query_params.get("date")
        try:
            day = _dt.date.fromisoformat(raw) if raw else timezone.localdate()
        except ValueError:
            return Response({"error": "Dates must look like 2026-10-27."}, status=status.HTTP_400_BAD_REQUEST)
        if day > timezone.localdate() or day < timezone.localdate() - _dt.timedelta(days=366):
            return Response({"error": "Choose a day within the last year."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(payment_ops.daily(day))
        except payment_ops.OpsError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class ReconcileLedgerView(APIView):
    """GET /api/payments/reconcile/ledger/?from=&to=   the money ledger for export (up to 92 days)."""

    throttle_classes = [ReconcileThrottle]

    def get(self, request):
        from . import reconciliation
        from .payment_report import ReportError, parse_range

        denied = _reconcile_access(request, "view_money")
        if denied:
            return denied
        try:
            date_from, date_to = parse_range(request.query_params.get("from"), request.query_params.get("to"))
        except ReportError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(reconciliation.ledger(date_from, date_to))


class ReconcileSweepView(APIView):
    """POST /api/payments/reconcile/sweep/   run the automatic check now."""

    throttle_classes = [MoneyActionThrottle]

    def post(self, request):
        from . import payment_ops

        denied = _reconcile_access(request, "run_check")
        if denied:
            return denied
        result = payment_ops.run_sweep(force=True)
        return Response({"result": result, "sweep": payment_ops.sweep_state()})


# ---------------------------------------------------------------------------
# Paid rescheduling (Check Appointment page)
# ---------------------------------------------------------------------------

class RescheduleQuoteView(APIView):
    """GET /api/payments/reschedule/quote/?ref_code=&doctor_id=   the reschedule service charge."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicLookupThrottle]

    def get(self, request):
        from .reschedule_pay import quote

        booking = Booking.objects.filter(ref_code=str(request.query_params.get("ref_code") or "").strip()).first()
        if booking is None:
            return Response({"error": "We couldn't find that booking."}, status=status.HTTP_404_NOT_FOUND)
        q = quote(booking, request.query_params.get("doctor_id"))
        return Response({"amount": float(q["amount"]), "percent": q["percent"], "base_fee": float(q["base_fee"]),
                         "currency": "NGN", "online_payments_enabled": payments.paystack_enabled()})


class RescheduleStartView(APIView):
    """
    POST /api/payments/reschedule/start/
      {"ref_code", "date", "time", "doctor_id"?, "reschedule_reason"?, "status": "Confirmed"}
    Checks the new date, then opens a Paystack checkout for the service charge
    (or moves the booking straight away when the clinic has no fee).
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicBookingThrottle]

    def post(self, request):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from . import reschedule_pay
        from .views import public_reschedule_problem

        ref_code = str(request.data.get("ref_code") or request.data.get("refCode") or "").strip()
        booking = Booking.objects.filter(ref_code=ref_code).first()
        if booking is None:
            return Response({"error": "We couldn't find that booking."}, status=status.HTTP_404_NOT_FOUND)
        payload = {k: request.data.get(k) for k in ("date", "time", "doctor_id", "doctorId", "reschedule_reason",
                                                     "rescheduleReason", "status") if request.data.get(k) not in (None, "")}
        payload.setdefault("status", "Confirmed")
        problem = public_reschedule_problem(booking, payload)
        if problem is not None:
            return problem
        if payload.get("doctorId") and not payload.get("doctor_id"):
            payload["doctor_id"] = payload.pop("doctorId")
        from .scheduling import parse_date as _pd
        new_day = _pd(payload.get("date") or booking.date)
        same_day = bool(new_day) and new_day.isoformat() == str(booking.date)
        same_time = str(payload.get("time") or booking.time or "").strip() == str(booking.time or "").strip()
        same_doctor = str(payload.get("doctor_id") or booking.doctor_id).strip().lower() == str(booking.doctor_id).strip().lower()
        if same_day and same_time and same_doctor:
            return Response({"error": "Your appointment is already on that date and time. Choose a different date to reschedule."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            result = reschedule_pay.start(request, booking, payload)
        except DRFValidationError as exc:
            return Response(flatten_error_detail(exc.detail), status=status.HTTP_400_BAD_REQUEST)
        except reschedule_pay.RescheduleError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if result.get("applied"):
            b = result["booking"]
            broadcast_booking_update(b, event_type="BOOKING_UPDATED", message=f"Booking {b.ref_code} rescheduled.")
            return Response({"applied": True, "amount": 0, "booking": public_booking_payload(b)})
        return Response({"authorization_url": result["authorization_url"], "reference": result["reference"],
                         "amount": float(result["amount"]), "percent": result["percent"],
                         "base_fee": float(result["base_fee"])}, status=status.HTTP_201_CREATED)


class RescheduleVerifyView(APIView):
    """GET /api/payments/reschedule/verify/?reference=   after Paystack sends the patient back."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicLookupThrottle]

    def get(self, request):
        from . import reschedule_pay
        from .models import ReschedulePayment

        reference = str(request.query_params.get("reference") or request.query_params.get("trxref") or "").strip()
        rp = ReschedulePayment.objects.filter(reference=reference).first()
        if rp is None:
            return Response({"error": "We couldn't find this reschedule payment."}, status=status.HTTP_404_NOT_FOUND)
        try:
            booking, outcome = reschedule_pay.confirm_reschedule(reference, source="verify", ip=client_ip(request))
        except payments.PaymentError as exc:
            return Response({"error": str(exc), "outcome": "unreachable"}, status=status.HTTP_502_BAD_GATEWAY)
        rp.refresh_from_db()
        return Response({
            "outcome": outcome,
            "reschedule": {k: v for k, v in reschedule_pay.reschedule_payload(rp).items() if k not in ("note",)},
            "message": rp.note if outcome in ("not_applied", "underpaid") else "",
            "booking": public_booking_payload(booking) if booking else None,
        })


class ReconcileLinkView(APIView):
    """
    POST /api/payments/reconcile/link/ {"reference", "ref_code", "note"}
    Tie a Paystack payment that matched no booking to the patient's booking and confirm it.
    """

    throttle_classes = [MoneyActionThrottle]

    def post(self, request):
        from . import reconciliation

        denied = _reconcile_access(request, "confirm")
        if denied:
            return denied
        try:
            result, outcome, booking = reconciliation.link_payment(
                request.data.get("reference"), request.data.get("ref_code"), request.data.get("note"),
                request.user, client_ip(request))
        except reconciliation.ReconcileError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if booking is not None and outcome == "paid":
            broadcast_booking_update(booking, event_type="NEW_BOOKING",
                                     message=f"Online payment linked and confirmed for {booking.patient_name} ({booking.ref_code}).")
        return Response(result)


class ReconcileResolveView(APIView):
    """
    POST /api/payments/reconcile/resolve/ {"reference", "action": "dismiss"|"refund", "note"}
    A payment with no booking: mark it as not a booking payment, or open a refund.
    """

    throttle_classes = [MoneyActionThrottle]

    def post(self, request):
        from . import reconciliation

        action = str(request.data.get("action") or "")
        denied = _reconcile_access(request, "record_refund" if action == "refund" else "confirm")
        if denied:
            return denied
        try:
            result = reconciliation.resolve_unmatched(request.data.get("reference"), action, request.data.get("note"),
                                                      request.user, client_ip(request))
        except reconciliation.ReconcileError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)
