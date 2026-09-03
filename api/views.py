import json
import random
import time
import datetime

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Q
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import (
    AllowAny,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
)

from rest_framework_simplejwt.tokens import RefreshToken, AccessToken

from .models import (
    Department,
    Doctor,
    SpecialistSchedule,
    Booking,
    HmoCompany,
    CustomTimeSlot,
    Role,
    UserProfile,
    AppSetting,
)

from .serializers import (
    DepartmentSerializer,
    DoctorSerializer,
    SpecialistScheduleSerializer,
    BookingSerializer,
    BookingListSerializer,
    HmoCompanySerializer,
    SystemUserSerializer,
    CustomTimeSlotSerializer,
    RoleSerializer,
    AppSettingSerializer,
)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def broadcast_booking_update(booking, event_type="BOOKING_UPDATE", message="", extra_data=None):
    """
    Broadcast a real-time booking event to the Channels layer for dashboard sync.
    Targeted to the 'hospital_feed' group and specific patient user channel if available.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        payload = {
            "event_type": event_type,
            "ref_code": getattr(booking, "ref_code", ""),
            "status": getattr(booking, "status", ""),
            "payment_status": getattr(booking, "payment_status", ""),
            "hmo_status": getattr(booking, "hmo_status", ""),
            "patient_name": getattr(booking, "patient_name", ""),
            "doctor_id": getattr(booking, "doctor_id", ""),
            "date": str(getattr(booking, "date", "")),
            "time_slot": getattr(booking, "time_slot", ""),
            "is_active": getattr(booking, "is_active", True),
            "message": message or f"Booking {getattr(booking, 'ref_code', '')} updated.",
            "timestamp": int(time.time() * 1000),
        }

        if extra_data and isinstance(extra_data, dict):
            payload.update(extra_data)

        # 1. Broadcast to the central live hospital dashboard
        async_to_sync(channel_layer.group_send)(
            "hospital_feed",
            {
                "type": "booking_update",
                "payload": payload,
            },
        )

        # 2. If booking is linked to a registered patient user, notify their private group
        patient_user_id = getattr(booking, "user_id", None)
        if patient_user_id:
            async_to_sync(channel_layer.group_send)(
                f"user_{patient_user_id}",
                {
                    "type": "booking_update",
                    "payload": payload,
                },
            )
    except Exception as e:
        # Prevent socket failures from breaking DB transactions
        print(f"[WebSocket Broadcast Warning] Failed to publish event: {e}")


def broadcast_bulk_refresh(action_name="BULK_UPDATE", message=""):
    """
    Broadcast a general trigger instructing hospital dashboards to invalidate caches or refresh.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        async_to_sync(channel_layer.group_send)(
            "hospital_feed",
            {
                "type": "booking_update",
                "payload": {
                    "event_type": action_name,
                    "message": message,
                    "timestamp": int(time.time() * 1000),
                },
            },
        )
    except Exception as e:
        print(f"[WebSocket Bulk Broadcast Warning] {e}")


def parse_bool_status(value, default=True):
    """
    Safely convert common frontend boolean/status values to bool.
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value != 0

    value = str(value).strip().lower()

    if value in (
        "true",
        "1",
        "yes",
        "active",
        "enabled",
        "enable",
        "on",
    ):
        return True

    if value in (
        "false",
        "0",
        "no",
        "disabled",
        "disable",
        "inactive",
        "off",
    ):
        return False

    return default


def safe_int(value, default=0, minimum=None):
    """
    Safely convert a value to integer.
    """
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    if minimum is not None:
        result = max(minimum, result)

    return result


def is_token_matching_day(token, day_name, day_short, occurrence):
    t = str(token).strip().lower()
    if not t:
        return False

    dn = day_name.lower()
    ds = day_short.lower()

    # Check if weekday is mentioned in token
    has_weekday = (
        dn in t
        or ds in t
        or t == dn
        or t == ds
        or dn.startswith(t)
        or ds.startswith(t)
    )
    if not has_weekday:
        return False

    # Check week occurrence constraints embedded in duty token text
    if "1st & 3rd" in t or "1st and 3rd" in t or "1st &3rd" in t:
        return occurrence in (1, 3)
    if "2nd & 4th" in t or "2nd and 4th" in t or "2nd &4th" in t:
        return occurrence in (2, 4)
    if "1st - 3rd" in t or "1st-3rd" in t or "1st to 3rd" in t:
        return occurrence in (1, 2, 3)
    if "1st" in t and not any(x in t for x in ("3rd", "2nd", "4th", "5th")):
        return occurrence == 1
    if "2nd" in t and not any(x in t for x in ("1st", "3rd", "4th", "5th")):
        return occurrence == 2
    if "3rd" in t and not any(x in t for x in ("1st", "2nd", "4th", "5th")):
        return occurrence == 3
    if "4th" in t and not any(x in t for x in ("1st", "2nd", "3rd", "5th")):
        return occurrence == 4
    if "5th" in t and not any(x in t for x in ("1st", "2nd", "3rd", "4th")):
        return occurrence == 5

    return True


def resolve_day_schedule(doctor, appointment_date):
    """
    Resolve a doctor's schedule for one calendar date.

    Honours duty_days, day_configs["weeks"] (alternating-week recurrence)
    and per-day capacity overrides, so every endpoint reports the same
    capacity the BookingSerializer actually enforces.
    """
    day_name = appointment_date.strftime("%A")
    day_short = appointment_date.strftime("%a")
    occurrence = (appointment_date.day - 1) // 7 + 1

    schedules = [
        s for s in doctor.schedules.all()
        if getattr(s, "status", True)
    ]

    if not schedules:
        doc_days = doctor.available_days or []
        on_duty = True
        if doc_days:
            on_duty = any(
                is_token_matching_day(d, day_name, day_short, occurrence)
                for d in doc_days
            )
        capacity = getattr(doctor, "capacity", 15) or 15
        return {
            "schedule": None,
            "capacity": safe_int(capacity, default=15, minimum=0),
            "on_duty": on_duty,
            "config": {},
            "has_schedule": False,
        }

    for sched in schedules:
        duty_tokens = [
            str(x).strip()
            for x in (sched.duty_days or [])
            if str(x).strip()
        ]
        if duty_tokens:
            on_weekday = any(
                is_token_matching_day(t, day_name, day_short, occurrence)
                for t in duty_tokens
            )
            if not on_weekday:
                continue

        configs = sched.day_configs or {}
        cfg = configs.get(day_short) or configs.get(day_name) or {}
        if not isinstance(cfg, dict):
            cfg = {}

        weeks = cfg.get("weeks") or []
        if weeks and occurrence not in weeks:
            continue

        capacity = safe_int(sched.capacity, default=15, minimum=0)
        if cfg.get("capacity") not in (None, ""):
            capacity = safe_int(cfg.get("capacity"), default=capacity, minimum=0)

        return {
            "schedule": sched,
            "capacity": capacity,
            "on_duty": True,
            "config": cfg,
            "has_schedule": True,
        }

    return {
        "schedule": schedules[0],
        "capacity": safe_int(schedules[0].capacity, default=15, minimum=0),
        "on_duty": False,
        "config": {},
        "has_schedule": True,
    }


def count_active_bookings(doctor, date_str=None, date_range=None):
    """
    Count live bookings for a doctor. Booking.doctor_id may hold either
    Doctor.doc_id or the numeric pk depending on which client wrote it.
    """
    doc_pk = getattr(doctor, "doc_id", None) or getattr(doctor, "id", None) or ""
    doctor_ids = [
        str(doc_pk).strip(),
    ]
    doctor_ids = [x for x in doctor_ids if x]
    doc_name = str(doctor.full_name or doctor.name or "").strip()

    filter_q = Q(doctor_id__in=doctor_ids)
    if doc_name:
        filter_q |= Q(doctor_name__icontains=doc_name)

    qs = (
        Booking.objects
        .filter(filter_q)
        .filter(is_active=True)
        .exclude(status__iexact="Disabled")
        .exclude(status__iexact="Cancelled")
    )
    if date_str:
        return qs.filter(date=date_str).count()
    if date_range:
        start, end = date_range
        return (
            qs.filter(date__gte=start.isoformat(), date__lte=end.isoformat())
            .values("date")
            .annotate(n=Count("ref_code"))
        )
    return qs.count()

def generate_id(prefix):
    """
    Generate a frontend-friendly unique ID.
    """
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(100, 999)}"


def is_staff_request(request):
    """
    Determine whether the request comes from an authenticated staff user.

    Supports:
        - Django session authentication
        - JWT Bearer authentication
        - Token-style Authorization headers
    """
    if (
        hasattr(request, "user")
        and request.user
        and request.user.is_authenticated
    ):
        return True

    auth_header = (
        request.headers.get("Authorization")
        or request.META.get("HTTP_AUTHORIZATION", "")
    )

    if not auth_header:
        return False

    parts = auth_header.split(" ", 1)

    if len(parts) != 2:
        return False

    scheme, token_str = parts

    if scheme.lower() not in ("bearer", "token"):
        return False

    token_str = token_str.strip()

    if not token_str or token_str.lower() in ("null", "undefined"):
        return False

    try:
        validated_token = AccessToken(token_str)
        user_id = validated_token.get("user_id")

        if not user_id:
            return False

        user = User.objects.filter(
            id=user_id,
            is_active=True,
        ).first()

        if not user:
            return False

        request.user = user
        return True

    except Exception:
        return False


def get_doctor_name(doctor):
    """
    Return the best display name for a doctor.
    """
    if not doctor:
        return "Specialist Doctor"

    return (
        doctor.full_name
        or doctor.acronym
        or "Specialist Doctor"
    )


def get_doctor_specialty(doctor):
    """
    Return the best specialty for a doctor.
    """
    if not doctor:
        return "General Medicine"

    if doctor.department:
        return doctor.department.name

    return doctor.specialty or "General Medicine"


def get_or_create_doctor_schedule(
    doctor,
    room=None,
    duty_days=None,
    shift_time=None,
    capacity=15,
    day_configs=None,
):
    """
    Get the doctor's first schedule or create one.

    IMPORTANT:
    Doctor.room_number is a property.
    Doctor.available_days is a property.
    Doctor.time_slots is a property.

    Therefore those properties must NEVER be assigned directly.
    Schedule information belongs to SpecialistSchedule.
    """
    if not doctor:
        return None

    schedule = (
        SpecialistSchedule.objects
        .filter(doctor=doctor)
        .order_by("sched_id")
        .first()
    )

    if schedule is None:
        schedule = SpecialistSchedule(
            sched_id=generate_id("sched"),
            doctor=doctor,
        )

    if room:
        schedule.room = room

    if duty_days is not None:
        schedule.duty_days = duty_days

    if shift_time:
        schedule.shift_time = shift_time

    if day_configs is not None:
        schedule.day_configs = day_configs

    schedule.capacity = safe_int(
        capacity,
        default=15,
        minimum=1,
    )

    if day_configs and isinstance(day_configs, dict):
        total_capacity = 0
        for config in day_configs.values():
            if isinstance(config, dict):
                total_capacity += safe_int(
                    config.get("capacity"),
                    default=schedule.capacity,
                    minimum=0,
                )

        if total_capacity <= 0:
            total_capacity = (
                schedule.capacity
                * max(1, len(duty_days or []))
            )
    else:
        total_capacity = (
            schedule.capacity
            * max(1, len(duty_days or []))
        )

    schedule.total_weekly_capacity = total_capacity
    schedule.doctor_name = get_doctor_name(doctor)
    schedule.specialty = get_doctor_specialty(doctor)
    schedule.status = True
    schedule.save()

    return schedule


# ============================================================
# STAFF LOGIN
# ============================================================

@method_decorator(csrf_exempt, name="dispatch")
class StaffLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        data = request.data or {}

        username_input = (
            data.get("username")
            or data.get("email")
            or data.get("user")
            or ""
        )

        password_input = (
            data.get("password")
            or data.get("pass")
            or ""
        )

        username_input = str(username_input).strip()
        password_input = str(password_input).strip()

        if not username_input or not password_input:
            return Response(
                {
                    "error": (
                        "Please enter both Email/Username "
                        "and Password."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_obj = (
            User.objects.filter(
                email__iexact=username_input
            ).first()
            or User.objects.filter(
                username__iexact=username_input
            ).first()
        )

        if user_obj and not user_obj.is_active:
            user_name = (
                user_obj.first_name
                or user_obj.username
            )
            return Response(
                {
                    "error": (
                        "Account Access Disabled: Staff account "
                        f"for '{user_name}' has been disabled "
                        "by the Administrator."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        user = authenticate(
            username=username_input,
            password=password_input,
        )

        if not user and user_obj:
            user = authenticate(
                username=user_obj.username,
                password=password_input,
            )

        if user and user.is_active:
            refresh = RefreshToken.for_user(user)

            role_name = (
                "Super Administrator"
                if user.is_superuser
                else "Helpdesk Officer"
            )

            desk_name = (
                "All Access"
                if user.is_superuser
                else "Helpdesk Reception"
            )

            try:
                profile = user.profile
                if profile and profile.role:
                    role_name = profile.role.name
                    desk_name = (
                        profile.role.primary_desk
                        or desk_name
                    )
            except UserProfile.DoesNotExist:
                pass

            user_display_name = (
                user.first_name
                or user.get_full_name()
                or user.username
            )

            return Response(
                {
                    "message": "Staff login successful",
                    "user": {
                        "id": user.id,
                        "username": user.username,
                        "email": (
                            user.email
                            or f"{user.username}@isaluhospitals.com"
                        ),
                        "name": user_display_name,
                        "role": role_name,
                        "desk": desk_name,
                    },
                    "tokens": {
                        "refresh": str(refresh),
                        "access": str(refresh.access_token),
                    },
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "error": (
                    "Invalid email or password. "
                    "Please check your credentials and try again."
                )
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )


# ============================================================
# DEPARTMENT
# ============================================================

class DepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = "dept_id"

    def get_queryset(self):
        queryset = (Department.objects.all().order_by("name"))

        if self.action in (
            "retrieve",
            "update",
            "partial_update",
            "destroy",
            "restore",
        ):
            return queryset

        include_disabled = (
            self.request.query_params.get(
                "include_disabled"
            )
            == "true"
        )

        status_param = (
            self.request.query_params.get("status")
        )

        search_param = (
            self.request.query_params.get("search")
        )

        if status_param:
            st = str(status_param).strip().lower()
            if st in ("active", "true", "1"):
                queryset = queryset.filter(status=True)
            elif st in (
                "disabled",
                "maintenance",
                "under maintenance",
                "inactive",
                "false",
                "0",
            ):
                queryset = queryset.filter(status=False)
        elif not include_disabled:
            queryset = queryset.filter(status=True)

        if search_param:
            q = str(search_param).strip()
            queryset = queryset.filter(
                Q(name__icontains=q)
                | Q(dept_id__icontains=q)
                | Q(description__icontains=q)
                | Q(location__icontains=q)
            )

        return queryset

    def destroy(self, request, *args, **kwargs):
        department = self.get_object()
        department.status = False
        department.save(update_fields=["status"])

        return Response(
            {
                "message": (
                    f"Department '{department.name}' "
                    "disabled successfully."
                ),
                "data": DepartmentSerializer(
                    department
                ).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="restore",
    )
    def restore(self, request, *args, **kwargs):
        department = self.get_object()
        department.status = True
        department.save(update_fields=["status"])

        return Response(
            {
                "message": (
                    f"Department '{department.name}' "
                    "restored successfully."
                ),
                "data": DepartmentSerializer(
                    department
                ).data,
            },
            status=status.HTTP_200_OK,
        )


# ============================================================
# DOCTOR
# ============================================================
class DoctorViewSet(viewsets.ModelViewSet):
    serializer_class = DoctorSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = 'doc_id'

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        val = self.kwargs.get(lookup_url_kwarg)
        if not val:
            return super().get_object()

        filter_q = Q(doc_id=val)
        if str(val).isdigit():
            filter_q |= Q(id=int(val))

        obj = queryset.filter(filter_q).first()
        if not obj:
            from django.http import Http404
            raise Http404(f"No Doctor matches the given query {val}.")
        self.check_object_permissions(self.request, obj)
        return obj

    @action(
        detail=True,
        methods=["get"],
        url_path="available-dates",
        permission_classes=[AllowAny],
    )
    def available_dates(self, request, *args, **kwargs):
        doctor = self.get_object()

        try:
            days_ahead = int(request.query_params.get("days", 90))
        except (TypeError, ValueError):
            days_ahead = 90
        days_ahead = max(1, min(days_ahead, 365))

        raw_from = request.query_params.get("from")
        try:
            start = (
                datetime.date.fromisoformat(raw_from)
                if raw_from else datetime.date.today()
            )
        except ValueError:
            start = datetime.date.today()

        end = start + datetime.timedelta(days=days_ahead - 1)

        booked_map = {
            str(row["date"]): row["n"]
            for row in count_active_bookings(doctor, date_range=(start, end))
        }

        schedule_count = len([
            s for s in doctor.schedules.all()
            if getattr(s, "status", True)
        ])

        dates = []
        availability_details = []

        for offset in range(days_ahead):
            day = start + datetime.timedelta(days=offset)
            date_str = day.isoformat()

            resolved = resolve_day_schedule(doctor, day)
            capacity = resolved["capacity"]
            booked = booked_map.get(date_str, 0)
            remaining = max(0, capacity - booked)
            is_full = booked >= capacity
            on_duty = bool(resolved["on_duty"])

            cfg = resolved["config"] or {}
            shift_times = cfg.get("shiftTimes") or cfg.get("shift_times") or []
            if isinstance(shift_times, str):
                shift_times = [shift_times]
            time_window = ", ".join(str(t) for t in shift_times if t)
            if not time_window and resolved["schedule"]:
                time_window = resolved["schedule"].shift_time or ""

            if on_duty:
                dates.append(date_str)

            availability_details.append({
                "date": date_str,
                "day": day.strftime("%A"),
                "booked": booked,
                "capacity": capacity,
                "remaining": remaining,
                "is_full": is_full,
                "isFull": is_full,
                "on_duty": on_duty,
                "onDuty": on_duty,
                "time_window": time_window,
                "timeWindow": time_window,
                "available": (not is_full) and bool(doctor.status) and on_duty,
            })

        return Response({
            "doctor_id": getattr(doctor, "doc_id", None),
            "doctor_status": bool(doctor.status),
            "schedule_count": schedule_count,
            "from": start.isoformat(),
            "days": days_ahead,
            "dates": dates,
            "availability": availability_details,
        })

    def get_queryset(self):
        queryset = (
            Doctor.objects
            .all()
            .select_related("department")
            .prefetch_related("schedules")
        )

        dept_param = (
            self.request.query_params.get("department")
            or self.request.query_params.get("department_id")
            or self.request.query_params.get("dept_id")
        )

        if dept_param and str(dept_param).strip().lower() != "all":
            dept_clean = str(dept_param).strip().lower()
            queryset = queryset.filter(
                department__dept_id__iexact=dept_clean
            )

        search = self.request.query_params.get("search")

        if search:
            search = str(search).strip()
            queryset = queryset.filter(
                Q(doc_id__icontains=search)
                | Q(name__icontains=search)
                | Q(full_name__icontains=search)
                | Q(acronym__icontains=search)
                | Q(specialty__icontains=search)
            )

        status_param = self.request.query_params.get("status")

        if status_param is not None:
            value = str(status_param).lower().strip()
            if value in ["active", "true", "1"]:
                queryset = queryset.filter(status=True)
            elif value in ["inactive", "disabled", "false", "0"]:
                queryset = queryset.filter(status=False)

        return queryset

    def sync_linked_schedules(self, doctor):
        if not doctor:
            return

        schedules = SpecialistSchedule.objects.filter(
            doctor=doctor
        )

        if not schedules.exists():
            schedules = SpecialistSchedule.objects.filter(
                doctor_name__icontains=doctor.name
            )

        doctor_display_name = (
            doctor.full_name
            or doctor.name
            or 'Specialist Doctor'
        )

        for schedule in schedules:
            schedule.doctor = doctor
            schedule.doctor_name = doctor_display_name

            if doctor.specialty:
                schedule.specialty = doctor.specialty

            if not schedule.room:
                schedule.room = "Consultation Suite 4B"

            schedule.save()

    def perform_create(self, serializer):
        doctor = serializer.save()
        self.sync_linked_schedules(doctor)

    def perform_update(self, serializer):
        doctor = serializer.save()
        self.sync_linked_schedules(doctor)

    def create(self, request, *args, **kwargs):
        data = request.data

        doc_id = (
            data.get('doc_id')
            or data.get('id')
            or f"doc-{int(time.time() * 1000)}-{random.randint(100, 999)}"
        )
        doc_id = str(doc_id).strip()

        name = (
            data.get('name')
            or data.get('fullName')
            or data.get('full_name')
            or 'Specialist Doctor'
        )

        full_name = (
            data.get('fullName')
            or data.get('full_name')
            or name
        )

        acronym = (
            data.get('acronym')
            or name
        )

        specialty = (
            data.get('specialty')
            or 'Specialist Consultation'
        )

        qualification = (
            data.get('qualification')
            or data.get('qualifications')
            or 'MBBS, FWACS'
        )

        accepted_types = (
            data.get('acceptedPatientTypes')
            or data.get('accepted_patient_types')
            or [
                'Private Self-Pay',
                'HMO Insurance'
            ]
        )

        dept_id = (
            data.get('departmentId')
            or data.get('department_id')
            or data.get('department')
        )

        department = None

        if isinstance(dept_id, dict):
            dept_value = (
                dept_id.get('dept_id')
                or dept_id.get('id')
                or dept_id.get('name')
            )
        else:
            dept_value = dept_id

        if dept_value:
            dept_value = str(dept_value).strip()
            department = (
                Department.objects
                .filter(dept_id__iexact=dept_value)
                .first()
            )

            if not department:
                department = (
                    Department.objects
                    .filter(name__iexact=dept_value)
                    .first()
                )

        if not department and specialty:
            department = (
                Department.objects
                .filter(name__icontains=str(specialty).strip())
                .first()
            )

        doctor = Doctor.objects.filter(
            doc_id=doc_id
        ).first()

        if doctor is None:
            doctor = Doctor(doc_id=doc_id)

        doctor.name = str(name).strip()
        doctor.full_name = str(full_name).strip()
        doctor.acronym = str(acronym).strip()
        doctor.specialty = str(specialty).strip()
        doctor.qualification = str(qualification).strip()
        doctor.qualifications = str(qualification).strip()
        doctor.accepted_patient_types = (
            accepted_types
            if isinstance(accepted_types, list)
            else [str(accepted_types)]
        )

        if department:
            doctor.department = department

        doctor.status = True
        doctor.save()

        room = (
            data.get('roomNumber')
            or data.get('room_number')
            or data.get('room')
            or 'Consultation Suite 4B'
        )

        available_days = (
            data.get('availableDays')
            or data.get('available_days')
            or data.get('availability')
            or [
                'Monday',
                'Wednesday',
                'Friday'
            ]
        )

        time_slots = (
            data.get('timeSlots')
            or data.get('time_slots')
            or [
                '08:00 AM – 02:00 PM'
            ]
        )

        if not isinstance(available_days, list):
            available_days = [available_days]

        if not isinstance(time_slots, list):
            time_slots = [time_slots]

        schedule_id = data.get('sched_id')

        if schedule_id:
            schedule = SpecialistSchedule.objects.filter(
                sched_id=str(schedule_id)
            ).first()
        else:
            schedule = SpecialistSchedule.objects.filter(
                doctor=doctor
            ).first()

        if schedule is None:
            schedule = SpecialistSchedule(
                sched_id=(
                    str(schedule_id)
                    if schedule_id
                    else f"sched-{int(time.time() * 1000)}-{random.randint(100, 999)}"
                )
            )

        schedule.doctor = doctor
        schedule.doctor_name = (
            doctor.full_name
            or doctor.name
        )
        schedule.specialty = (
            doctor.specialty
            or specialty
        )
        schedule.room = str(room).strip()
        schedule.duty_days = available_days
        schedule.shift_time = (
            time_slots[0]
            if time_slots
            else '08:00 AM – 02:00 PM'
        )
        schedule.capacity = int(
            data.get('capacity') or 15
        )

        day_configs = (
            data.get('day_configs')
            or data.get('dayConfigs')
            or {}
        )

        if not isinstance(day_configs, dict):
            day_configs = {}

        schedule.day_configs = day_configs

        if day_configs:
            total_capacity = 0
            for config in day_configs.values():
                if isinstance(config, dict):
                    try:
                        total_capacity += int(
                            config.get(
                                'capacity',
                                schedule.capacity
                            )
                        )
                    except (TypeError, ValueError):
                        total_capacity += schedule.capacity

            if total_capacity <= 0:
                total_capacity = (
                    schedule.capacity
                    * max(1, len(available_days))
                )
        else:
            total_capacity = (
                schedule.capacity
                * max(1, len(available_days))
            )

        schedule.total_weekly_capacity = total_capacity
        schedule.status = parse_bool_status(
            data.get('status', True)
        )
        schedule.save()

        serializer = self.get_serializer(doctor)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED
        )

# ============================================================
# SPECIALIST SCHEDULE
# ============================================================

class SpecialistScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = SpecialistScheduleSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = "sched_id"

    def get_queryset(self):
        return (
            SpecialistSchedule.objects
            .all()
            .select_related(
                "doctor",
                "doctor__department",
            )
            .order_by("sched_id")
        )

    def sync_doctor_from_schedule(self, schedule):
        if not schedule:
            return

        doctor = schedule.doctor

        if (
            not doctor
            and getattr(
                schedule,
                "doctor_name",
                None,
            )
        ):
            doctor_name = str(schedule.doctor_name).strip()
            doctor = (
                Doctor.objects
                .filter(name__iexact=doctor_name)
                .first()
            )
            if not doctor:
                doctor = (
                    Doctor.objects
                    .filter(full_name__iexact=doctor_name)
                    .first()
                )

        if doctor and not schedule.doctor:
            schedule.doctor = doctor
            schedule.doctor_name = get_doctor_name(doctor)
            schedule.specialty = get_doctor_specialty(doctor)
            schedule.save()

    @action(
        detail=False,
        methods=["get"],
        url_path="capacity-analytics",
    )
    def capacity_analytics(self, request):
        schedules = (
            SpecialistSchedule.objects
            .filter(status=True)
        )

        total_weekly_capacity = 0
        for schedule in schedules:
            if schedule.total_weekly_capacity:
                total_weekly_capacity += schedule.total_weekly_capacity
            else:
                total_weekly_capacity += (
                    schedule.capacity
                    * max(1, len(schedule.duty_days or []))
                )

        active_schedule_count = schedules.count()
        total_bookings = (
            Booking.objects
            .filter(is_active=True)
            .exclude(status="Disabled")
            .count()
        )

        utilization_pct = round(
            (
                total_bookings
                / max(1, total_weekly_capacity)
            )
            * 100,
            1,
        )

        overbooked = []
        for schedule in schedules:
            if not schedule.doctor:
                continue

            doctor_name = get_doctor_name(schedule.doctor)
            booking_count = (
                Booking.objects
                .filter(
                    doctor_id=schedule.doctor.doc_id,
                    is_active=True,
                )
                .exclude(status="Disabled")
                .count()
            )

            capacity = (
                schedule.total_weekly_capacity
                or (
                    schedule.capacity
                    * max(1, len(schedule.duty_days or []))
                )
            )

            if booking_count > capacity:
                overbooked.append(
                    {
                        "sched_id": schedule.sched_id,
                        "doctorId": schedule.doctor.doc_id,
                        "doctorName": doctor_name,
                        "bookedCount": booking_count,
                        "capacity": capacity,
                    }
                )

        return Response(
            {
                "totalConfiguredCapacity": total_weekly_capacity,
                "activeSchedulesCount": active_schedule_count,
                "totalActiveBookings": total_bookings,
                "facilityCapacityUtilizationPct": utilization_pct,
                "overbookedSchedulesCount": len(overbooked),
                "overbookedSchedules": overbooked,
            },
            status=status.HTTP_200_OK,
        )

    def perform_create(self, serializer):
        schedule = serializer.save()
        self.sync_doctor_from_schedule(schedule)

    def perform_update(self, serializer):
        schedule = serializer.save()
        self.sync_doctor_from_schedule(schedule)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        data = request.data

        sched_id = (
            data.get("sched_id")
            or data.get("id")
            or generate_id("sched")
        )
        sched_id = str(sched_id).strip()

        doc_id = (
            data.get("doctor_id")
            or data.get("doctorId")
            or data.get("doctor")
        )

        if isinstance(doc_id, dict):
            doc_id = (
                doc_id.get("doc_id")
                or doc_id.get("id")
            )

        doc_name = (
            data.get("doctor_name")
            or data.get("doctorName")
            or ""
        )

        specialty = (
            data.get("specialty")
            or "General Medicine"
        )

        room = (
            data.get("room")
            or data.get("roomNumber")
            or data.get("room_number")
            or "Consultation Suite"
        )

        duty_days = (
            data.get("duty_days")
            or data.get("dutyDays")
            or data.get("availableDays")
            or []
        )

        day_configs = (
            data.get("day_configs")
            or data.get("dayConfigs")
            or {}
        )

        shift_time = (
            data.get("shift_time")
            or data.get("shiftTime")
            or data.get("time")
            or "08:00 AM – 02:00 PM"
        )

        capacity = safe_int(
            data.get("capacity"),
            default=15,
            minimum=1,
        )

        doctor = None

        if doc_id:
            doctor = (
                Doctor.objects
                .filter(doc_id__iexact=str(doc_id).strip())
                .first()
            )

        if not doctor and doc_name:
            doctor = (
                Doctor.objects
                .filter(name__iexact=str(doc_name).strip())
                .first()
            )
            if not doctor:
                doctor = (
                    Doctor.objects
                    .filter(full_name__iexact=str(doc_name).strip())
                    .first()
                )

        if not doctor:
            return Response(
                {
                    "error": (
                        "The selected doctor does not exist "
                        "in the hospital database. Create the "
                        "doctor first, then assign a schedule."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if day_configs and isinstance(day_configs, dict):
            total_capacity = 0
            for config in day_configs.values():
                if isinstance(config, dict):
                    total_capacity += safe_int(
                        config.get("capacity"),
                        default=capacity,
                        minimum=0,
                    )

            if total_capacity <= 0:
                total_capacity = (
                    capacity * max(1, len(duty_days))
                )
        else:
            total_capacity = (
                capacity * max(1, len(duty_days))
            )

        schedule = (
            SpecialistSchedule.objects
            .filter(sched_id=sched_id)
            .first()
        )

        if not schedule:
            schedule = SpecialistSchedule(sched_id=sched_id)

        schedule.doctor = doctor
        schedule.doctor_name = get_doctor_name(doctor)
        schedule.specialty = (
            specialty or get_doctor_specialty(doctor)
        )
        schedule.room = str(room).strip()
        schedule.duty_days = duty_days
        schedule.day_configs = day_configs
        schedule.shift_time = str(shift_time).strip()
        schedule.capacity = capacity
        schedule.total_weekly_capacity = total_capacity
        schedule.status = parse_bool_status(
            data.get("status"),
            default=True,
        )
        schedule.save()

        serializer = self.get_serializer(schedule)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
        )


# ============================================================
# BOOKING
# ============================================================

class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [AllowAny]
    lookup_field = "ref_code"

    def get_serializer_class(self):
        if self.action == "list":
            return BookingListSerializer
        return BookingSerializer

    def perform_authentication(self, request):
        try:
            super().perform_authentication(request)
        except Exception:
            from django.contrib.auth.models import AnonymousUser
            request.user = AnonymousUser()

    def get_permissions(self):
        if self.action in ("update", "partial_update", "destroy"):
            return [IsAuthenticated()]
        return [AllowAny()]

    def get_queryset(self):
        include_disabled = (
            self.request.query_params.get(
                "include_disabled"
            )
            == "true"
        )

        queryset = Booking.objects.all()

        if include_disabled:
            return queryset

        return (
            queryset
            .filter(is_active=True)
            .exclude(status="Disabled")
        )

    def perform_create(self, serializer):
        booking = serializer.save()
        broadcast_booking_update(
            booking,
            event_type="NEW_BOOKING",
            message=f"New appointment booked for {booking.patient_name} ({booking.ref_code})."
        )

    def list(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {
                    "detail": (
                        "Authentication required. Access to "
                        "patient booking registry is restricted "
                        "to authorized hospital staff."
                    )
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return super().list(
            request,
            *args,
            **kwargs,
        )

    def destroy(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {
                    "detail": (
                        "Authentication required. Only "
                        "authorized hospital staff can "
                        "delete or disable appointment records."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        booking = self.get_object()

        reason = (
            request.data.get("reason")
            or request.data.get("delete_reason")
            or request.data.get("deleteReason")
            or "Disabled by Administrator"
        )

        booking.is_active = False
        booking.status = "Disabled"
        booking.delete_reason = reason
        booking.save()

        # Real-time WebSocket event
        broadcast_booking_update(
            booking,
            event_type="BOOKING_DISABLED",
            message=f"Booking {booking.ref_code} was disabled."
        )

        return Response(
            {
                "message": (
                    f"Booking {booking.ref_code} "
                    "disabled successfully."
                ),
                "data": BookingSerializer(
                    booking
                ).data,
            },
            status=status.HTTP_200_OK,
        )

    def _validate_completion_payment(
        self,
        booking,
        request,
    ):
        new_status = request.data.get("status")

        if (
            new_status == "Completed"
            and booking.payment_status == "Pending"
        ):
            return Response(
                {
                    "error": (
                        "Payment Clearance Required: Ticket "
                        f"{booking.ref_code} cannot be marked as "
                        "Completed while payment status is Pending."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return None

    def partial_update(
        self,
        request,
        *args,
        **kwargs,
    ):
        booking = self.get_object()

        error = self._validate_completion_payment(
            booking,
            request,
        )

        if error:
            return error

        response = super().partial_update(
            request,
            *args,
            **kwargs,
        )

        # Real-time WebSocket event
        booking.refresh_from_db()
        broadcast_booking_update(
            booking,
            event_type="BOOKING_UPDATED",
            message=f"Booking {booking.ref_code} updated."
        )

        return response

    def update(
        self,
        request,
        *args,
        **kwargs,
    ):
        booking = self.get_object()

        error = self._validate_completion_payment(
            booking,
            request,
        )

        if error:
            return error

        response = super().update(
            request,
            *args,
            **kwargs,
        )

        # Real-time WebSocket event
        booking.refresh_from_db()
        broadcast_booking_update(
            booking,
            event_type="BOOKING_UPDATED",
            message=f"Booking {booking.ref_code} fully updated."
        )

        return response

    @action(
        detail=True,
        methods=["post"],
        url_path="send-reminder",
    )
    def send_reminder(self, request, ref_code=None):
        """Sends Email and SMS reminder to patient for a single appointment."""
        booking = self.get_object()
        force = request.data.get("force", False)

        from api.notification_service import send_single_booking_reminder
        res = send_single_booking_reminder(booking, force=force)

        # Real-time WebSocket event
        broadcast_booking_update(
            booking,
            event_type="REMINDER_SENT",
            message=f"Reminder sent to {booking.patient_name} ({booking.ref_code})."
        )

        return Response(res, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["post"],
        url_path="send-bulk-reminders",
    )
    def send_bulk_reminders(self, request):
        """Triggers bulk Email + SMS reminders for upcoming appointments."""
        target_date = request.data.get("target_date") or request.data.get("date")
        days_ahead = int(request.data.get("days_ahead", 1))
        force = request.data.get("force", False)

        from api.notification_service import process_appointment_reminders
        summary = process_appointment_reminders(
            target_date=target_date,
            days_ahead=days_ahead,
            force=force
        )

        return Response(summary, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["get"],
        url_path="summary",
    )
    def summary(self, request):
        total = Booking.objects.count()

        checked_in = (
            Booking.objects
            .filter(status="Checked In")
            .count()
        )

        pending_hmo = (
            Booking.objects
            .filter(payment_type="HMO Insurance")
            .exclude(hmo_status="Approved")
            .count()
        )

        pending_cash = (
            Booking.objects
            .filter(payment_type="Private Self-Pay")
            .exclude(payment_status="Cleared")
            .count()
        )

        return Response(
            {
                "totalBookings": total,
                "checkedInCount": checked_in,
                "pendingHmoCount": pending_hmo,
                "pendingCashCount": pending_cash,
            }
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="clear-all",
    )
    def clear_all(self, request):
        if not is_staff_request(request):
            return Response(
                {
                    "detail": (
                        "Authentication required."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        reason = (
            request.data.get("reason")
            or "Cleared by authorized administrator"
        )

        count = (
            Booking.objects
            .filter(is_active=True)
            .update(
                is_active=False,
                status="Disabled",
                delete_reason=reason,
            )
        )

        # Real-time WebSocket bulk refresh event
        broadcast_bulk_refresh(
            action_name="BOOKINGS_CLEARED",
            message=f"{count} booking records were disabled by administrator."
        )

        return Response(
            {
                "message": (
                    f"{count} booking records disabled."
                ),
                "count": count,
            }
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="availability",
        permission_classes=[AllowAny],
    )
    def availability(self, request):
        doctor_id = str(
            request.query_params.get(
                "doctor_id"
            )
            or ""
        ).strip()

        date_str = str(
            request.query_params.get(
                "date"
            )
            or ""
        ).strip()

        if not doctor_id or not date_str:
            return Response(
                {
                    "error": (
                        "doctor_id and date are required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            appointment_date = datetime.datetime.strptime(
                date_str,
                "%Y-%m-%d",
            ).date()
        except ValueError:
            return Response(
                {
                    "error": (
                        "Invalid appointment date. "
                        "Use YYYY-MM-DD."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        doctor = (
            Doctor.objects
            .filter(doc_id__iexact=doctor_id)
            .first()
        )

        if not doctor:
            return Response(
                {
                    "error": "Doctor not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        resolved = resolve_day_schedule(doctor, appointment_date)
        capacity = resolved["capacity"]
        on_duty = resolved["on_duty"]

        booked = count_active_bookings(doctor, date_str=date_str)
        remaining = max(0, capacity - booked)
        is_full = booked >= capacity

        return Response({
            "doctorId": doctor.doc_id,
            "date": date_str,
            "booked": booked,
            "capacity": capacity,
            "remaining": remaining,
            "is_full": is_full,
            "available": (not is_full) and bool(doctor.status) and on_duty,
            "onDuty": on_duty,
        })

    @action(
        detail=False,
        methods=["get"],
        url_path="public-lookup",
        permission_classes=[AllowAny],
    )
    def public_lookup(self, request):
        ref_code = str(
            request.query_params.get(
                "ref_code"
            )
            or ""
        ).strip()

        phone = str(
            request.query_params.get(
                "phone"
            )
            or ""
        ).strip()

        if not ref_code and not phone:
            return Response(
                {
                    "error": (
                        "Booking reference or phone number "
                        "is required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if ref_code:
            booking = (
                Booking.objects
                .filter(ref_code__iexact=ref_code)
                .first()
            )
        else:
            booking = (
                Booking.objects
                .filter(patient_phone__iexact=phone)
                .order_by("-created_at")
                .first()
            )

        if not booking:
            return Response(
                {
                    "error": "Appointment not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if (
            phone
            and booking.patient_phone.strip() != phone
        ):
            return Response(
                {
                    "error": (
                        "Appointment details could not "
                        "be verified."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            BookingSerializer(booking).data
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="disabled",
    )
    def disabled_bookings(self, request):
        queryset = Booking.objects.filter(
            Q(is_active=False)
            | Q(status="Disabled")
        )

        serializer = self.get_serializer(
            queryset,
            many=True,
        )

        return Response(
            serializer.data
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="restore",
    )
    def restore_booking(
        self,
        request,
        ref_code=None,
    ):
        booking = (
            Booking.objects
            .filter(ref_code=ref_code)
            .first()
        )

        if not booking:
            return Response(
                {
                    "error": (
                        "Booking record not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        booking.is_active = True
        booking.status = "Booked"
        booking.delete_reason = ""
        booking.save()

        # Real-time WebSocket event
        broadcast_booking_update(
            booking,
            event_type="BOOKING_RESTORED",
            message=f"Booking {booking.ref_code} restored successfully."
        )

        return Response(
            {
                "message": (
                    f"Booking {booking.ref_code} "
                    "restored successfully."
                ),
                "data": BookingSerializer(
                    booking
                ).data,
            }
        )

    @action(
        detail=True,
        methods=["post", "patch"],
        url_path="reroute-cashdesk",
    )
    def reroute_cashdesk(
        self,
        request,
        ref_code=None,
    ):
        booking = (
            Booking.objects
            .filter(ref_code=ref_code)
            .first()
        )

        if not booking:
            return Response(
                {
                    "error": (
                        f"Booking ticket {ref_code} "
                        "not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        remark = (
            request.data.get("remark")
            or request.data.get("delete_reason")
            or request.data.get("hmoRemark")
            or request.data.get("hmo_status")
            or "Passed from HMO to Cashdesk"
        )

        booking.payment_type = "Private Self-Pay"
        booking.hmo_name = "N/A"
        booking.hmo_status = (
            "Re-routed to Cashdesk "
            f"(Self-Pay): {remark}"
        )
        booking.payment_status = "Pending"
        booking.delete_reason = (
            "Re-routed from HMO to Cashdesk: "
            f"{remark}"
        )
        booking.save()

        # Real-time WebSocket event
        broadcast_booking_update(
            booking,
            event_type="BOOKING_REROUTED_CASHDESK",
            message=f"Ticket {booking.ref_code} re-routed to Cashdesk."
        )

        return Response(
            {
                "message": (
                    f"Ticket {booking.ref_code} "
                    "re-routed to Cashdesk as "
                    "Private Self-Pay."
                ),
                "data": BookingSerializer(
                    booking
                ).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="check-in",
    )
    def check_in(
        self,
        request,
        ref_code=None,
    ):
        booking = self.get_object()

        if (
            booking.payment_type == "HMO Insurance"
            and booking.hmo_status != "Approved"
        ):
            return Response(
                {
                    "error": (
                        "HMO Approval Required: Cannot "
                        f"check in ticket {booking.ref_code} "
                        f"while HMO status is "
                        f"{booking.hmo_status or 'Awaiting Approval'}. "
                        "Route patient to HMO Desk first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if booking.payment_status == "Pending":
            return Response(
                {
                    "error": (
                        "Payment Clearance Required: Cannot "
                        f"check in ticket {booking.ref_code} "
                        "while payment is Pending. Route "
                        "patient to Cashdesk first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        booking.status = "Checked In"
        booking.save()

        # Real-time WebSocket event
        broadcast_booking_update(
            booking,
            event_type="PATIENT_CHECKED_IN",
            message=f"Patient {booking.patient_name} ({booking.ref_code}) checked in."
        )

        return Response(
            {
                "message": (
                    f"Patient {booking.patient_name} "
                    "checked in successfully."
                ),
                "data": BookingSerializer(
                    booking
                ).data,
            }
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="approve-hmo",
    )
    def approve_hmo(
        self,
        request,
        ref_code=None,
    ):
        booking = self.get_object()

        policy = (
            request.data.get("policyCode")
            or booking.hmo_policy_code
            or f"POL-{random.randint(100000, 999999)}"
        )

        auth = (
            request.data.get("authCode")
            or booking.hmo_auth_code
            or f"AUTH-{random.randint(1000, 9999)}"
        )

        booking.hmo_policy_code = policy
        booking.hmo_auth_code = auth
        booking.hmo_status = "Approved"
        booking.payment_status = "Cleared"
        booking.save()

        # Real-time WebSocket event
        broadcast_booking_update(
            booking,
            event_type="HMO_APPROVED",
            message=f"HMO pre-authorization approved for ticket {booking.ref_code}."
        )

        return Response(
            {
                "message": (
                    "Pre-Authorization cleared "
                    f"for ticket {booking.ref_code}."
                ),
                "authCode": auth,
                "data": BookingSerializer(
                    booking
                ).data,
            }
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="pay-cashdesk",
    )
    def pay_cashdesk(
        self,
        request,
        ref_code=None,
    ):
        booking = self.get_object()

        method = (
            request.data.get(
                "paymentMethod"
            )
            or "POS Card Terminal"
        )

        invoice = (
            f"INV-{random.randint(100000, 999999)}"
        )

        booking.payment_status = "Cleared"
        booking.payment_method = method
        booking.invoice_ref = invoice
        booking.save()

        # Real-time WebSocket event
        broadcast_booking_update(
            booking,
            event_type="PAYMENT_CLEARED",
            message=f"Cashdesk payment cleared via {method} for ticket {booking.ref_code}."
        )

        return Response(
            {
                "message": (
                    f"Cashdesk payment cleared "
                    f"via {method}."
                ),
                "invoiceRef": invoice,
                "data": BookingSerializer(
                    booking
                ).data,
            }
        )


# ============================================================
# HMO COMPANY
# ============================================================

class HmoCompanyViewSet(viewsets.ModelViewSet):
    queryset = (
        HmoCompany.objects
        .all()
        .order_by("name")
    )
    serializer_class = HmoCompanySerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = "hmo_id"

    def create(self, request, *args, **kwargs):
        hmo_id = (
            request.data.get("hmo_id")
            or request.data.get("id")
        )
        name = request.data.get("name")

        if hmo_id:
            existing = (
                HmoCompany.objects
                .filter(hmo_id=hmo_id)
                .first()
            )
            if existing:
                serializer = self.get_serializer(
                    existing,
                    data=request.data,
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(
                    serializer.data,
                    status=status.HTTP_200_OK,
                )

        if name:
            existing = (
                HmoCompany.objects
                .filter(
                    name__iexact=str(name).strip()
                )
                .first()
            )
            if existing:
                serializer = self.get_serializer(
                    existing,
                    data=request.data,
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(
                    serializer.data,
                    status=status.HTTP_200_OK,
                )

        return super().create(
            request,
            *args,
            **kwargs,
        )


# ============================================================
# SYSTEM USERS
# ============================================================

class SystemUserViewSet(viewsets.ModelViewSet):
    queryset = (
        User.objects
        .all()
        .order_by(
            "-date_joined",
            "-id",
        )
    )
    serializer_class = SystemUserSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"

    def get_object(self):
        lookup_url_kwarg = (
            self.lookup_url_kwarg
            or self.lookup_field
        )
        value = str(self.kwargs[lookup_url_kwarg])

        if value.startswith("usr-"):
            value = value[4:]

        try:
            user = User.objects.get(id=int(value))
        except (
            User.DoesNotExist,
            ValueError,
        ):
            from django.http import Http404
            raise Http404("System user not found.")

        self.check_object_permissions(
            self.request,
            user,
        )
        return user

    def list(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {
                    "detail": (
                        "Authentication required. Access "
                        "to system staff user directory is "
                        "restricted to authorized staff "
                        "administrators."
                    )
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return super().list(
            request,
            *args,
            **kwargs,
        )

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {
                    "detail": (
                        "Authentication required. Only "
                        "authorized staff administrators "
                        "can create system user accounts."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        data = request.data
        email = str(
            data.get("email") or ""
        ).strip().lower()

        raw_password = (
            data.get("password")
            or "admin123"
        )

        name = str(
            data.get("name")
            or data.get("first_name")
            or (
                email.split("@")[0]
                if email
                else "Staff User"
            )
        ).strip()

        role_name = str(
            data.get("role")
            or "Helpdesk Officer"
        ).strip()

        status_input = data.get(
            "status",
            "Active",
        )

        username = (
            email
            if email
            else generate_id("user")
        )

        user = None
        if email:
            user = (
                User.objects
                .filter(email__iexact=email)
                .first()
            )

        if not user:
            user = (
                User.objects
                .filter(username__iexact=username)
                .first()
            )

        is_active = parse_bool_status(
            status_input,
            default=True,
        )

        if user:
            user.first_name = name
            if email:
                user.email = email
            if raw_password:
                user.set_password(raw_password)
            user.is_staff = True
            user.is_active = is_active
            user.save()
        else:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=raw_password,
                first_name=name,
                is_staff=True,
                is_active=is_active,
            )

        role_obj = (
            Role.objects
            .filter(name__iexact=role_name)
            .first()
        )

        if not role_obj:
            role_obj = (
                Role.objects
                .filter(role_id__iexact=role_name)
                .first()
            )

        if not role_obj:
            role_obj = (
                Role.objects
                .filter(name__icontains=role_name)
                .first()
            )

        if not role_obj:
            role_id_clean = (
                role_name
                .lower()
                .replace(" ", "-")
            )
            role_obj, _ = (
                Role.objects
                .get_or_create(
                    role_id=role_id_clean,
                    defaults={
                        "name": role_name,
                        "primary_desk": "helpdesk",
                        "status": True,
                    },
                )
            )

        profile, _ = (
            UserProfile.objects
            .get_or_create(user=user)
        )
        profile.role = role_obj
        profile.save()

        serializer = self.get_serializer(user)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {
                    "detail": (
                        "Authentication required. Only "
                        "authorized staff administrators can "
                        "deactivate system user accounts."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        user = self.get_object()
        user.is_active = False
        user.save(update_fields=["is_active"])

        return Response(
            {
                "message": (
                    f"User {user.username} "
                    "deactivated successfully."
                )
            },
            status=status.HTTP_200_OK,
        )


# ============================================================
# CUSTOM TIME SLOTS
# ============================================================

class CustomTimeSlotViewSet(viewsets.ModelViewSet):
    queryset = CustomTimeSlot.objects.all()
    serializer_class = CustomTimeSlotSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = "slot_id"


# ============================================================
# ROLES
# ============================================================

class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = "role_id"

    def get_object(self):
        lookup_url_kwarg = (
            self.lookup_url_kwarg
            or self.lookup_field
        )
        value = str(self.kwargs[lookup_url_kwarg]).strip()

        role = (
            Role.objects
            .filter(role_id=value)
            .first()
        )

        if not role:
            role = (
                Role.objects
                .filter(name__iexact=value)
                .first()
            )

        if not role:
            from django.http import Http404
            raise Http404("Role not found.")

        self.check_object_permissions(
            self.request,
            role,
        )
        return role

    def create(self, request, *args, **kwargs):
        role_id = (
            request.data.get("role_id")
            or request.data.get("id")
        )
        name = request.data.get("name")

        if role_id:
            existing = (
                Role.objects
                .filter(role_id=role_id)
                .first()
            )
            if existing:
                serializer = self.get_serializer(
                    existing,
                    data=request.data,
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(
                    serializer.data,
                    status=status.HTTP_200_OK,
                )

        if name:
            existing = (
                Role.objects
                .filter(name__iexact=str(name).strip())
                .first()
            )
            if existing:
                serializer = self.get_serializer(
                    existing,
                    data=request.data,
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(
                    serializer.data,
                    status=status.HTTP_200_OK,
                )

        return super().create(
            request,
            *args,
            **kwargs,
        )


# ============================================================
# AI / EXECUTIVE REPORT
# ============================================================

class AiReportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        prompt = str(
            request.data.get("prompt")
            or "Generate Full Executive Board Report"
        ).strip()

        active = (
            Booking.objects
            .filter(is_active=True)
            .exclude(status="Disabled")
        )

        total = active.count()
        checked_in = active.filter(status="Checked In").count()
        completed = active.filter(status="Completed").count()
        pending_hmo = (
            active
            .filter(payment_type="HMO Insurance")
            .exclude(hmo_status="Approved")
            .count()
        )
        pending_cash = (
            active
            .filter(payment_type="Private Self-Pay")
            .exclude(payment_status="Cleared")
            .count()
        )

        departments = list(
            Department.objects
            .filter(status=True)
            .values("name", "dept_id")
        )

        doctors = list(
            Doctor.objects
            .filter(status=True)
            .values("doc_id", "name", "full_name", "specialty")
        )

        top = (
            active
            .values("doctor_specialty")
            .annotate(n=Count("ref_code"))
            .order_by("-n")
            .first()
        )

        generated_at = (
            timezone.localtime(timezone.now()).isoformat()
        )

        report = (
            "ISALU HOSPITALS - "
            "BACKEND GENERATED EXECUTIVE REPORT\n"
            f"Generated: {generated_at}\n"
            f"Query: {prompt}\n\n"
            "HOSPITAL METRICS\n"
            f"- Active bookings: {total}\n"
            f"- Checked in: {checked_in}\n"
            f"- Completed: {completed}\n"
            f"- Pending HMO: {pending_hmo}\n"
            f"- Pending cashdesk: {pending_cash}\n"
            f"- Active departments: {len(departments)}\n"
            f"- Active doctors: {len(doctors)}\n"
            f"- Top specialty by booking volume: "
            f"{(top or {}).get('doctor_specialty') or 'N/A'}\n\n"
            "RECOMMENDATIONS\n"
            "- Review pending HMO authorizations promptly.\n"
            "- Monitor cashdesk clearance before completing consultations.\n"
            "- Use server-side schedule capacity as the authoritative availability source.\n"
        )

        return Response(
            {
                "prompt": prompt,
                "report": report,
                "generatedAt": generated_at,
            }
        )


# ============================================================
# APP SETTINGS
# ============================================================

class AppSettingViewSet(viewsets.ModelViewSet):
    queryset = AppSetting.objects.all()
    serializer_class = AppSettingSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = "key"


# ============================================================
# CUSTOM TOKEN REFRESH
# ============================================================

@method_decorator(csrf_exempt, name="dispatch")
class CustomTokenRefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        refresh_token = str(
            request.data.get("refresh")
            or request.data.get("refresh_token")
            or ""
        ).strip()

        if not refresh_token:
            return Response(
                {
                    "error": "Refresh token is required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            refresh = RefreshToken(refresh_token)
            return Response(
                {
                    "access": str(refresh.access_token),
                    "refresh": str(refresh),
                    "expires_in": 86400,
                },
                status=status.HTTP_200_OK,
            )
        except Exception:
            return Response(
                {
                    "error": "Invalid or expired refresh token."
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )


# ============================================================
# HOSPITAL EVENT STREAM (FALLBACK SSE COMPATIBILITY)
# ============================================================

@method_decorator(csrf_exempt, name="dispatch")
class HospitalEventStreamView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        def event_stream():
            connected_event = {
                "type": "CONNECTED",
                "timestamp": int(time.time() * 1000),
            }
            yield "data: " + json.dumps(connected_event) + "\n\n"

            for _ in range(3):
                time.sleep(5)
                heartbeat_event = {
                    "type": "HEARTBEAT",
                    "timestamp": int(time.time() * 1000),
                }
                yield "data: " + json.dumps(heartbeat_event) + "\n\n"

        response = StreamingHttpResponse(
            event_stream(),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response