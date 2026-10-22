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
from rest_framework.views import APIView

from . import payments
from .models import Booking, Doctor
from .scheduling import AWAITING_PAYMENT
from .serializers import BookingSerializer
from .views import PublicBookingThrottle, PublicLookupThrottle, broadcast_booking_update, flatten_error_detail, public_booking_payload

logger = logging.getLogger(__name__)


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
            booking, outcome = payments.confirm_payment(reference)
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
        return Response({"outcome": outcome, "booking": public_booking_payload(booking)})


class PaystackWebhookView(APIView):
    """Paystack calls this after a successful charge. Signed with the secret key."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        raw = request.body
        if not payments.valid_webhook_signature(raw, request.headers.get("X-Paystack-Signature", "")):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        try:
            event = json.loads(raw.decode() or "{}")
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)
        if event.get("event") == "charge.success":
            data = event.get("data") or {}
            try:
                # Re-check with Paystack rather than trusting the payload alone.
                booking, outcome = payments.confirm_payment(data.get("reference"))
                if booking is not None and outcome == "paid":
                    broadcast_booking_update(booking, event_type="NEW_BOOKING",
                                             message=f"New paid appointment for {booking.patient_name} ({booking.ref_code}).")
            except payments.PaymentError:
                logger.exception("Webhook verification failed for %s", data.get("reference"))
                return Response(status=status.HTTP_502_BAD_GATEWAY)  # Paystack retries
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
