"""
Walk-in registration (Helpdesk).

A patient who comes to the hospital is registered by the Helpdesk for a
doctor's clinic day:

- Only days the doctor actually holds clinic, from the roster (with one-off
  changes: cancelled / moved clinics), from today to WALK_IN_DAYS days ahead.
- No other booking rule applies: no slot/capacity limit, no same-day cutoff,
  no HMO 48-hour window, no online-payment requirement, no double-booking
  block, no accepted-patient-type limit. The only rule is the visit-type
  order (duplicates.auto_visit_type): First visit, Follow-up, Follow-up...
  for private, HMO and Company patients alike.
- A walk-in never takes or shows on an online booking slot
  (scheduling.slot_bookings_qs leaves walk-ins out).
- Private Self-Pay: unpaid, so it goes to the Cashdesk to pay.
  HMO / Company: goes to the HMO desk for approval.
"""
import datetime
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import payments
from .duplicates import auto_visit_type, follow_ups_per_first_visit, normalize_phone
from .models import Booking, Doctor
from .scheduling import parse_date, resolve_doctor_day, slot_bookings_qs

WALK_IN_DAYS = 30
PAY_TYPES = ("Private Self-Pay", "HMO Insurance", "Company")


class WalkInError(ValueError):
    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


def _doctor(doctor_id):
    key = str(doctor_id or "").strip()
    if not key:
        return None
    return Doctor.objects.select_related("department").filter(doc_id__iexact=key).first()


def _clinic_name(doctor):
    return doctor.department.name if doctor.department_id and doctor.department else (doctor.specialty or "General Medicine")


def _shifts(resolved):
    return [s.strip() for s in str(resolved.get("shift") or "").split(",") if s.strip()]


def normalize_pay_type(value):
    v = " ".join(str(value or "").split()).lower()
    if "company" in v or "corporate" in v:
        return "Company"
    if "hmo" in v or "insurance" in v:
        return "HMO Insurance"
    if v in ("", "private", "private self-pay", "self-pay", "self pay", "cash"):
        return "Private Self-Pay"
    return None


def clinic_days(doctor, today=None):
    """The doctor's clinic days from today to WALK_IN_DAYS ahead, from the roster."""
    today = today or timezone.localdate()
    out = []
    for i in range(WALK_IN_DAYS + 1):
        day = today + datetime.timedelta(days=i)
        r = resolve_doctor_day(doctor, day)
        if not r["on_duty"]:
            continue
        iso = day.isoformat()
        slots_used = slot_bookings_qs().filter(doctor_id=doctor.doc_id, date=iso).count()
        walk_ins = Booking.objects.filter(doctor_id=doctor.doc_id, date=iso, is_walk_in=True, is_active=True).exclude(
            status__in=("Cancelled", "Disabled")).count()
        out.append({
            "date": iso,
            "label": day.strftime("%a, %d %b %Y"),
            "is_today": i == 0,
            "shifts": _shifts(r),
            "note": r.get("note") or "",
            # For information only: walk-ins don't use or change the online slots.
            "online_capacity": r.get("capacity") or 0,
            "online_booked": slots_used,
            "walk_ins": walk_ins,
        })
    return out


def check_day(doctor, date_val, today=None):
    """(date, resolved) for a valid walk-in day, else WalkInError."""
    today = today or timezone.localdate()
    day = parse_date(date_val)
    if day is None:
        raise WalkInError("Choose the clinic day.", field="date")
    if day < today:
        raise WalkInError("Walk-ins can't be registered for a past date.", field="date")
    if day > today + datetime.timedelta(days=WALK_IN_DAYS):
        raise WalkInError(f"Walk-ins can be registered for clinic days within the next {WALK_IN_DAYS} days only.", field="date")
    r = resolve_doctor_day(doctor, day)
    if not r["on_duty"]:
        why = r.get("note") or "the doctor has no clinic on that day"
        raise WalkInError(f"{doctor.acronym or doctor.name} doesn't hold clinic on {day.strftime('%A, %d %b %Y')} ({why}). "
                          "Choose one of the clinic days listed.", field="date")
    return day, r


def preview_visit_type(doctor, name, phone, card, pay_type):
    visit, _first, _ = auto_visit_type(doctor, name, phone, card, payment_type=pay_type)
    order = "First visit, Follow-up, Follow-up" if follow_ups_per_first_visit(pay_type) == 2 else "First visit, Follow-up"
    return visit, f"In each clinic bookings go {order}, then First visit again."


def register(data, staff_name=""):
    """Create the walk-in booking. Returns the Booking; raises WalkInError."""
    doctor = _doctor(data.get("doctor_id") or data.get("doctorId"))
    if doctor is None:
        raise WalkInError("Choose the doctor.", field="doctor_id")
    if doctor.status is False:
        raise WalkInError(f"{doctor.acronym or doctor.name} is not active.", field="doctor_id")

    name = " ".join(str(data.get("patient_name") or data.get("patientName") or "").split())[:200]
    phone = " ".join(str(data.get("patient_phone") or data.get("patientPhone") or "").split())[:40]
    if len(name) < 2:
        raise WalkInError("Enter the patient's full name.", field="patient_name")
    if len(normalize_phone(phone)) < 7:
        raise WalkInError("Enter the patient's phone number.", field="patient_phone")

    pay_type = normalize_pay_type(data.get("payment_type") or data.get("paymentType"))
    if pay_type is None:
        raise WalkInError("Choose Private Self-Pay, HMO or Company.", field="payment_type")
    hmo_name = " ".join(str(data.get("hmo_name") or data.get("hmoName") or data.get("company_name") or "").split())[:200]
    policy = " ".join(str(data.get("hmo_policy_code") or data.get("hmoPolicyCode") or "").split())[:100]
    if pay_type == "HMO Insurance" and not hmo_name:
        raise WalkInError("Choose the patient's HMO.", field="hmo_name")
    if pay_type == "Company" and not hmo_name:
        raise WalkInError("Choose the patient's company.", field="hmo_name")

    day, resolved = check_day(doctor, data.get("date"))
    shifts = _shifts(resolved)
    time_str = " ".join(str(data.get("time") or "").split())
    if not time_str or (shifts and time_str not in shifts):
        time_str = shifts[0] if shifts else "Walk-in"

    card = " ".join(str(data.get("hospital_card_number") or data.get("hospitalCardNumber") or "").split())[:30]
    with transaction.atomic():
        # Lock the doctor row so two registrations at once get the right visit-type order.
        Doctor.objects.select_for_update().filter(doc_id=doctor.doc_id).first()
        visit, _note = preview_visit_type(doctor, name, phone, card, pay_type)
        asked = data.get("visit_type") or data.get("visitType")
        if asked and payments.normalize_visit_type(asked) != visit:
            raise WalkInError(f"This registration is a {visit.lower()} (visit-type order for this patient in this clinic).",
                              field="visit_type", visit_type_required=visit)
        sponsored = pay_type in ("HMO Insurance", "Company")
        while True:
            ref = f"ISALU-{uuid.uuid4().hex[:10].upper()}"
            if not Booking.objects.filter(ref_code=ref).exists():
                break
        booking = Booking.objects.create(
            ref_code=ref,
            doctor_id=doctor.doc_id,
            doctor_name=doctor.full_name or doctor.name,
            doctor_specialty=_clinic_name(doctor),
            date=day.isoformat(),
            time=time_str,
            patient_name=name,
            patient_phone=phone,
            patient_email=" ".join(str(data.get("patient_email") or data.get("patientEmail") or "").split())[:254],
            hospital_card_number=card,
            reason=str(data.get("reason") or "").strip()[:1000] or "Walk-in",
            payment_type=pay_type,
            visit_type=visit,
            status="Confirmed",
            is_active=True,
            # Private: pay at the Cashdesk. HMO/Company: HMO desk approval (as for HMO bookings).
            payment_status=("Company Cover" if pay_type == "Company" else "HMO Cover") if sponsored else "Pending",
            payment_method="POS / Cash",
            hmo_status="Pending Pre-Auth" if sponsored else "N/A",
            hmo_name=hmo_name if sponsored else "N/A",
            hmo_policy_code=policy if pay_type == "HMO Insurance" else "",
            first_visit_fee=payments.first_visit_fee(doctor) if visit == payments.FOLLOW_UP else None,
            is_walk_in=True,
            walk_in_registered_by=str(staff_name or "")[:200],
        )
    return booking


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _allowed(request):
    from .views import is_staff_request, user_has_desk, is_monitor_only
    # The Helpdesk registers walk-ins (or a role given the Walk-in Registration module),
    # unless an administrator took the module away from this user (Module Access).
    from .views import is_admin_user, user_desk_overrides
    if not (is_staff_request(request) and not is_monitor_only(request.user)):
        return False
    if is_admin_user(request.user):
        return True
    if "walk_in" in user_desk_overrides(request.user)[1]:
        return False
    return user_has_desk(request.user, "helpdesk") or user_has_desk(request.user, "walk_in")


def _denied():
    return Response({"error": "Walk-in registration is done at the Helpdesk."}, status=status.HTTP_403_FORBIDDEN)


def _staff_name(user):
    return (user.get_full_name() or user.first_name or user.username) if user and user.is_authenticated else ""


class WalkInClinicDaysView(APIView):
    """GET ?doctor_id= -> the doctor's clinic days for the next 30 days."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _allowed(request):
            return _denied()
        doctor = _doctor(request.query_params.get("doctor_id"))
        if doctor is None:
            return Response({"error": "Choose the doctor."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"doctor_id": doctor.doc_id, "days_ahead": WALK_IN_DAYS, "clinic": _clinic_name(doctor),
                         "days": clinic_days(doctor)})


class WalkInVisitTypeView(APIView):
    """GET ?doctor_id&patient_name&patient_phone&hospital_card_number&payment_type -> visit type."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _allowed(request):
            return _denied()
        q = request.query_params
        doctor = _doctor(q.get("doctor_id"))
        pay_type = normalize_pay_type(q.get("payment_type")) or "Private Self-Pay"
        if doctor is None:
            return Response({"visit_type": payments.FIRST_VISIT, "message": ""})
        visit, note = preview_visit_type(doctor, q.get("patient_name", ""), q.get("patient_phone", ""),
                                         q.get("hospital_card_number", ""), pay_type)
        fee = payments.clinic_fee(doctor, visit) if pay_type == "Private Self-Pay" else None
        return Response({"visit_type": visit, "message": note, "fee": float(fee) if fee is not None else None})


class WalkInRegisterView(APIView):
    """POST -> register a walk-in."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _allowed(request):
            return _denied()
        try:
            booking = register(request.data, _staff_name(request.user))
        except WalkInError as exc:
            return Response({"error": str(exc), **exc.extra}, status=status.HTTP_400_BAD_REQUEST)
        from .serializers import BookingSerializer
        from .views import broadcast_booking_update
        sponsored = booking.payment_type in ("HMO Insurance", "Company")
        broadcast_booking_update(
            booking, event_type="NEW_BOOKING",
            message=f"Walk-in registered: {booking.patient_name} ({booking.ref_code}).",
        )
        route = "HMO desk for approval" if sponsored else "Cashdesk to pay"
        return Response({
            "message": f"{booking.patient_name} registered as a walk-in ({booking.visit_type}). Send the patient to the {route}.",
            "route": "hmo" if sponsored else "cashdesk",
            "data": BookingSerializer(booking).data,
        }, status=status.HTTP_201_CREATED)
