import datetime
import json
import random
import time

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, StreamingHttpResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import AnonRateThrottle
from rest_framework.permissions import (
    SAFE_METHODS,
    BasePermission,
    AllowAny,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from .models import (
    AppSetting,
    Booking,
    CustomTimeSlot,
    Department,
    Doctor,
    HmoCompany,
    Role,
    ScheduleException,
    SpecialistSchedule,
)
from .schedule_changes import (
    ScheduleChangeError,
    apply_schedule_exception,
    preview_schedule_change,
    retry_failed_notifications,
    undo_schedule_exception,
)
from .scheduling import (
    capacity_bookings_qs,
    count_doctor_bookings,
    parse_date,
    resolve_doctor_day,
    safe_int,
)
from .serializers import (
    AppSettingSerializer,
    BookingListSerializer,
    BookingSerializer,
    CustomTimeSlotSerializer,
    DepartmentSerializer,
    DoctorSerializer,
    HmoCompanySerializer,
    RoleSerializer,
    ScheduleExceptionSerializer,
    SpecialistScheduleSerializer,
    SystemUserSerializer,
    parse_bool_status,
)


# ============================================================
# DASHBOARD / REDIS CACHE
# ============================================================
#
# The booking list is cached under keys that include query parameters, so
# deleting a single fixed key never invalidated it (the dashboard showed
# stale data for up to 60s after every change). A version number is now
# embedded in every key; bumping it invalidates all variants at once.

DASHBOARD_CACHE_TIMEOUT = 30
BOOKINGS_CACHE_VERSION_KEY = "isalu:dashboard:bookings:version"
BOOKINGS_LIST_CACHE_KEY = "isalu:dashboard:bookings:list:v2"
BOOKING_SUMMARY_CACHE_KEY = "isalu:dashboard:bookings:summary:v2"


def _cache_version():
    try:
        version = cache.get(BOOKINGS_CACHE_VERSION_KEY)
        if version is None:
            version = int(time.time() * 1000)
            cache.set(BOOKINGS_CACHE_VERSION_KEY, version, None)
        return version
    except Exception as exc:
        print(f"[Cache Warning] Version read failed: {exc}")
        return 0


def versioned_key(base, *parts):
    suffix = ":".join(str(p) for p in parts)
    return f"{base}:v{_cache_version()}" + (f":{suffix}" if suffix else "")


def invalidate_dashboard_cache():
    """Invalidate every cached booking list / summary variant."""
    try:
        cache.set(BOOKINGS_CACHE_VERSION_KEY, int(time.time() * 1000) + random.randint(1, 999), None)
    except Exception as exc:
        print(f"[Cache Warning] Dashboard cache invalidation failed: {exc}")


def get_cached_response(key):
    try:
        return cache.get(key)
    except Exception as exc:
        print(f"[Cache Warning] Cache read failed for {key}: {exc}")
        return None


def set_cached_response(key, value, timeout=DASHBOARD_CACHE_TIMEOUT):
    try:
        cache.set(key, value, timeout)
    except Exception as exc:
        print(f"[Cache Warning] Cache write failed for {key}: {exc}")


# ============================================================
# REAL-TIME BROADCAST
# ============================================================

def broadcast_booking_update(booking, event_type="BOOKING_UPDATE", message="", extra_data=None):
    """Publish a booking event to the 'hospital_feed' group."""
    invalidate_dashboard_cache()

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
            "time_slot": getattr(booking, "time", ""),
            "is_active": getattr(booking, "is_active", True),
            "message": message or f"Booking {getattr(booking, 'ref_code', '')} updated.",
            "timestamp": int(time.time() * 1000),
        }
        if extra_data and isinstance(extra_data, dict):
            payload.update(extra_data)

        async_to_sync(channel_layer.group_send)(
            "hospital_feed",
            {"type": "booking_update", "payload": payload},
        )
    except Exception as e:
        # Socket failures must never break database writes.
        print(f"[WebSocket Broadcast Warning] Failed to publish event: {e}")


def broadcast_bulk_refresh(action_name="BULK_UPDATE", message=""):
    invalidate_dashboard_cache()
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


# ============================================================
# HELPERS
# ============================================================

def generate_id(prefix):
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(100, 999)}"


def is_staff_request(request):
    """
    True when the request carries a valid session or JWT for an active user.
    Works even on AllowAny views, where DRF authentication may be skipped.
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_active:
        return True

    auth_header = (
        request.headers.get("Authorization")
        or request.META.get("HTTP_AUTHORIZATION", "")
    )
    if not auth_header:
        return False

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() not in ("bearer", "token"):
        return False

    token_str = parts[1].strip()
    if not token_str or token_str.lower() in ("null", "undefined"):
        return False

    try:
        validated_token = AccessToken(token_str)
        user_id = validated_token.get("user_id")
        if not user_id:
            return False
        user = User.objects.filter(id=user_id, is_active=True).first()
        if not user:
            return False
        request.user = user
        return True
    except Exception:
        return False


ADMIN_ROLE_KEYWORDS = ("super administrator", "super admin", "hospital administrator", "system administrator")
ALL_DESKS = [
    "helpdesk", "hmo", "cashdesk", "analytics", "monitor", "users", "all_patients",
    "checked_in_patients", "hmo_enrollees", "private_patients",
    "create_specialist_schedule", "clinic", "disabled_bookings", "hmo_declined",
]


def get_user_role(user):
    try:
        return user.profile.role
    except Exception:
        return None


def is_admin_user(user):
    """
    Administrators may edit and delete records. A Django superuser is always
    an admin; otherwise the user's role name decides (e.g. "Super Administrator").
    """
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser:
        return True
    role = get_user_role(user)
    name = str(getattr(role, "name", "") or "").strip().lower()
    return bool(role and role.status and any(k in name for k in ADMIN_ROLE_KEYWORDS))


def build_staff_profile(user):
    """Profile returned at login and by /auth/me/ (drives the dashboard sidebar)."""
    role = get_user_role(user)
    admin = is_admin_user(user)
    if role:
        role_name = role.name
        desk = role.primary_desk or "helpdesk"
        allowed = [d for d in (role.allowed_desks or []) if d in ALL_DESKS]
        if desk in ALL_DESKS and desk not in allowed:
            allowed.insert(0, desk)
        if not role.status:
            allowed = []
    else:
        role_name = "Super Administrator" if user.is_superuser else "Helpdesk Officer"
        desk = "analytics" if user.is_superuser else "helpdesk"
        allowed = [] if user.is_superuser else ["helpdesk", "all_patients", "checked_in_patients"]
    if admin:
        allowed = list(ALL_DESKS)
    else:
        if "hmo" in allowed and "hmo_declined" not in allowed:
            # Whoever works the HMO desk also handles the requests it declined.
            allowed.append("hmo_declined")
        if "monitor" in allowed and "all_patients" not in allowed:
            # Monitor operators look patients up in the All Patients Directory.
            allowed.append("all_patients")
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email or f"{user.username}@isaluhospitals.com",
        "name": user.first_name or user.get_full_name() or user.username,
        "role": role_name,
        "desk": desk,
        "isAdmin": admin,
        "allowedDesks": allowed,
    }


def admin_required_response(action_text="perform this action"):
    return Response(
        {
            "detail": f"Only administrators can {action_text}.",
            "error": f"Only administrators can {action_text}.",
        },
        status=status.HTTP_403_FORBIDDEN,
    )


class AdminOrReadOnly(BasePermission):
    """Anyone may read; only administrators may create, edit or delete."""
    message = "Only administrators can make changes here."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return is_admin_user(request.user)


class StaffReadAdminWrite(BasePermission):
    """Signed-in staff may read; only administrators may make changes."""
    message = "Only administrators can make changes here."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return is_admin_user(request.user)


def user_has_desk(user, desk):
    if is_admin_user(user):
        return True
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return desk in build_staff_profile(user)["allowedDesks"]


class RosterCreateAdminChange(BasePermission):
    """
    Anyone may read. Staff whose role includes the Specialist Roster module
    may create; only administrators may edit or delete.
    """
    message = "Only administrators can edit or delete schedules."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if request.method == "POST" and getattr(view, "action", None) == "create":
            return user_has_desk(request.user, "create_specialist_schedule")
        return is_admin_user(request.user)


class StaffCreateAdminChange(BasePermission):
    """Anyone may read, signed-in staff may create, administrators may edit/delete."""
    message = "Only administrators can edit or delete these records."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if request.method == "POST":
            return bool(request.user and request.user.is_authenticated)
        return is_admin_user(request.user)


class LoginRateThrottle(AnonRateThrottle):
    scope = "login"


class PublicBookingThrottle(AnonRateThrottle):
    scope = "public_booking"


class PublicLookupThrottle(AnonRateThrottle):
    scope = "public_lookup"


def staff_required_response(action_text="perform this action"):
    return Response(
        {
            "detail": (
                "Authentication required. Only authorized hospital staff "
                f"can {action_text}."
            ),
            "error": "Authentication required.",
        },
        status=status.HTTP_401_UNAUTHORIZED,
    )


def get_doctor_name(doctor):
    if not doctor:
        return "Specialist Doctor"
    return doctor.full_name or doctor.name or doctor.acronym or "Specialist Doctor"


def get_doctor_specialty(doctor):
    if not doctor:
        return "General Medicine"
    if doctor.department:
        return doctor.department.name
    return doctor.specialty or "General Medicine"


def resolve_day_schedule(doctor, appointment_date):
    """Backwards-compatible wrapper around api.scheduling.resolve_doctor_day."""
    return resolve_doctor_day(doctor, appointment_date)


def count_active_bookings(doctor, date_str=None, date_range=None):
    """
    Count capacity-consuming bookings for a doctor.

    Matches strictly on Booking.doctor_id == Doctor.doc_id. The previous
    implementation also matched doctor_name__icontains, which counted other
    doctors' bookings when one name contained another.
    """
    qs = capacity_bookings_qs().filter(doctor_id=doctor.doc_id)
    if date_str:
        return qs.filter(date=str(date_str)).count()
    if date_range:
        start, end = date_range
        return (
            qs.filter(date__gte=start.isoformat(), date__lte=end.isoformat())
            .values("date")
            .annotate(n=Count("ref_code"))
        )
    return qs.count()


def first_error_message(errors, fallback="Invalid request."):
    """Flatten DRF error dicts into one human-readable message."""
    if isinstance(errors, dict):
        if "error" in errors:
            value = errors["error"]
            return value[0] if isinstance(value, list) else str(value)
        for field, value in errors.items():
            if isinstance(value, list) and value:
                return f"{field}: {value[0]}"
            if value:
                return f"{field}: {value}"
    if isinstance(errors, list) and errors:
        return str(errors[0])
    return fallback


def error_response(serializer_errors, http_status=status.HTTP_400_BAD_REQUEST):
    body = {"error": first_error_message(serializer_errors)}
    if isinstance(serializer_errors, dict):
        for key, value in serializer_errors.items():
            if key != "error":
                body[key] = value
    return Response(body, status=http_status)


# ============================================================
# STAFF LOGIN
# ============================================================

@method_decorator(csrf_exempt, name="dispatch")
class StaffLoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]
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

            return Response(
                {
                    "message": "Staff login successful",
                    "user": build_staff_profile(user),
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


def mask_ref(ref_code):
    """ISALU-89A46973F6 -> ISALU-******73F6 (enough to recognise, not to act on)."""
    ref = str(ref_code or "")
    return ref[:6] + "*" * max(0, len(ref) - 10) + ref[-4:] if len(ref) > 10 else ref


# Fields never returned to anonymous callers.
PUBLIC_HIDDEN_FIELDS = (
    "referral_doc_data", "referral_doc_text", "referralDocData", "referralDocText",
    "patient_email", "patientEmail", "hmo_auth_code", "hmoAuthCode", "hmo_policy_code",
    "hmoPolicyCode", "delete_reason", "deleteReason", "invoice_ref", "invoiceRef",
)


def mask_enrollee_id(value):
    """Show only the last 4 characters of an HMO enrollee ID to the public."""
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * min(6, len(value) - 4) + value[-4:]


def public_booking_payload(booking):
    data = dict(BookingSerializer(booking).data)
    for key in PUBLIC_HIDDEN_FIELDS:
        data.pop(key, None)
    # Patients see enough of their enrollee ID to recognise it, never all of it.
    data["hmoEnrolleeIdMasked"] = mask_enrollee_id(getattr(booking, "hmo_policy_code", ""))
    return data


class StaffProfileView(APIView):
    """Current staff profile, so role/module changes apply without re-login."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"user": build_staff_profile(request.user)})


# ============================================================
# DEPARTMENT
# ============================================================

class DepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer
    permission_classes = [AdminOrReadOnly]
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
    permission_classes = [RosterCreateAdminChange]
    lookup_field = "doc_id"

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        value = str(self.kwargs.get(self.lookup_url_kwarg or self.lookup_field) or "").strip()
        # Doctor's primary key is doc_id (there is no numeric id column).
        obj = queryset.filter(doc_id=value).first() or queryset.filter(doc_id__iexact=value).first()
        if not obj:
            raise Http404(f"No Doctor matches the given query {value}.")
        self.check_object_permissions(self.request, obj)
        return obj

    def get_queryset(self):
        queryset = (
            Doctor.objects.all()
            .select_related("department")
            .prefetch_related("schedules", "schedule_exceptions")
        )

        dept_param = (
            self.request.query_params.get("department")
            or self.request.query_params.get("department_id")
            or self.request.query_params.get("dept_id")
        )
        if dept_param and str(dept_param).strip().lower() != "all":
            queryset = queryset.filter(department__dept_id__iexact=str(dept_param).strip())

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
            if value in ("active", "true", "1"):
                queryset = queryset.filter(status=True)
            elif value in ("inactive", "disabled", "false", "0"):
                queryset = queryset.filter(status=False)

        return queryset

    @action(
        detail=True,
        methods=["get"],
        url_path="available-dates",
        permission_classes=[AllowAny],
    )
    def available_dates(self, request, *args, **kwargs):
        doctor = self.get_object()

        days_ahead = max(1, min(safe_int(request.query_params.get("days"), 90), 365))
        today = timezone.localdate()
        start = parse_date(request.query_params.get("from")) or today
        end = start + datetime.timedelta(days=days_ahead - 1)

        booked_map = {
            str(row["date"]): row["n"]
            for row in count_active_bookings(doctor, date_range=(start, end))
        }
        schedule_count = len([s for s in doctor.schedules.all() if s.status])

        dates = []
        availability_details = []
        for offset in range(days_ahead):
            day = start + datetime.timedelta(days=offset)
            date_str = day.isoformat()

            resolved = resolve_doctor_day(doctor, day)
            on_duty = bool(resolved["on_duty"])
            capacity = resolved["capacity"] if on_duty else 0
            booked = booked_map.get(date_str, 0)
            remaining = max(0, capacity - booked)
            is_full = on_duty and booked >= capacity
            is_past = day < today

            if on_duty and not is_past:
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
                "time_window": resolved["shift"],
                "timeWindow": resolved["shift"],
                "note": resolved["note"],
                "available": (
                    on_duty and not is_full and not is_past and bool(doctor.status)
                ),
            })

        return Response({
            "doctor_id": doctor.doc_id,
            "doctor_status": bool(doctor.status),
            "schedule_count": schedule_count,
            "from": start.isoformat(),
            "days": days_ahead,
            "dates": dates,
            "availability": availability_details,
        })

    def sync_linked_schedules(self, doctor):
        """Keep the denormalised name/specialty on this doctor's schedules current."""
        if not doctor:
            return
        for schedule in SpecialistSchedule.objects.filter(doctor=doctor):
            schedule.save()  # SpecialistSchedule.save() mirrors the doctor

    def perform_update(self, serializer):
        doctor = serializer.save()
        self.sync_linked_schedules(doctor)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """
        Register a doctor. A schedule is created only when duty days are
        supplied; otherwise the doctor is saved without one and becomes
        bookable once a schedule is assigned in the Specialist Roster.
        (Previously Mon/Wed/Fri was silently invented for every new doctor.)
        """
        data = request.data

        doc_id = str(data.get("doc_id") or data.get("id") or generate_id("doc")).strip()
        if Doctor.objects.filter(doc_id__iexact=doc_id).exists():
            return Response(
                {"error": f"A doctor with ID '{doc_id}' already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        payload = dict(data.items()) if hasattr(data, "items") else dict(data)
        payload["doc_id"] = doc_id
        payload.setdefault("status", True)
        if not payload.get("department") and not payload.get("departmentId") and payload.get("specialty"):
            payload["department"] = payload["specialty"]
        if not payload.get("acronym"):
            payload["acronym"] = payload.get("name") or ""

        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        doctor = serializer.save()

        duty_days = (
            data.get("availableDays")
            or data.get("available_days")
            or data.get("availability")
            or data.get("duty_days")
            or []
        )
        if duty_days:
            time_slots = data.get("timeSlots") or data.get("time_slots") or []
            if isinstance(time_slots, str):
                time_slots = [time_slots]
            schedule_serializer = SpecialistScheduleSerializer(data={
                "doctor": doctor.doc_id,
                "room": (
                    data.get("roomNumber") or data.get("room_number")
                    or data.get("room") or "Consultation Suite"
                ),
                "duty_days": duty_days,
                "day_configs": data.get("day_configs") or data.get("dayConfigs") or {},
                "shift_time": time_slots[0] if time_slots else "08:00 AM – 02:00 PM",
                "capacity": data.get("capacity") or 15,
                "status": True,
            })
            schedule_serializer.is_valid(raise_exception=True)
            schedule_serializer.save()

        doctor = self.get_queryset().get(doc_id=doctor.doc_id)
        return Response(self.get_serializer(doctor).data, status=status.HTTP_201_CREATED)


# ============================================================
# SPECIALIST SCHEDULE
# ============================================================

class SpecialistScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = SpecialistScheduleSerializer
    permission_classes = [RosterCreateAdminChange]
    lookup_field = "sched_id"

    def get_queryset(self):
        queryset = (
            SpecialistSchedule.objects.all()
            .select_related("doctor", "doctor__department")
            .order_by("sched_id")
        )
        doctor_param = self.request.query_params.get("doctor") or self.request.query_params.get("doctor_id")
        if doctor_param:
            queryset = queryset.filter(doctor__doc_id__iexact=str(doctor_param).strip())
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return error_response(serializer.errors)
        if SpecialistSchedule.objects.filter(sched_id=serializer.validated_data["sched_id"]).exists():
            return Response(
                {"error": "A schedule with this ID already exists. Refresh and try again."},
                status=status.HTTP_409_CONFLICT,
            )
        schedule = serializer.save()
        return Response(self.get_serializer(schedule).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        if not serializer.is_valid():
            return error_response(serializer.errors)
        schedule = serializer.save()
        return Response(self.get_serializer(schedule).data)

    @action(detail=False, methods=["get"], url_path="capacity-analytics")
    def capacity_analytics(self, request):
        """Weekly capacity vs. bookings in the current Monday–Sunday week."""
        schedules = list(
            SpecialistSchedule.objects.filter(status=True).select_related("doctor")
        )
        total_weekly_capacity = sum(
            s.total_weekly_capacity or s.capacity for s in schedules
        )

        today = timezone.localdate()
        week_start = today - datetime.timedelta(days=today.weekday())
        week_end = week_start + datetime.timedelta(days=6)
        week_qs = capacity_bookings_qs().filter(
            date__gte=week_start.isoformat(), date__lte=week_end.isoformat()
        )
        total_bookings = week_qs.count()

        booking_counts = {
            str(row["doctor_id"]): row["n"]
            for row in week_qs.values("doctor_id").annotate(n=Count("ref_code"))
        }

        capacity_by_doctor = {}
        for schedule in schedules:
            if not schedule.doctor:
                continue
            key = schedule.doctor.doc_id
            entry = capacity_by_doctor.setdefault(key, {
                "sched_id": schedule.sched_id,
                "doctorId": key,
                "doctorName": get_doctor_name(schedule.doctor),
                "capacity": 0,
            })
            entry["capacity"] += schedule.total_weekly_capacity or schedule.capacity

        overbooked = []
        for key, entry in capacity_by_doctor.items():
            booked = booking_counts.get(key, 0)
            if booked > entry["capacity"]:
                overbooked.append({**entry, "bookedCount": booked})

        return Response({
            "weekStart": week_start.isoformat(),
            "weekEnd": week_end.isoformat(),
            "totalConfiguredCapacity": total_weekly_capacity,
            "activeSchedulesCount": len(schedules),
            "totalActiveBookings": total_bookings,
            "facilityCapacityUtilizationPct": round(
                (total_bookings / max(1, total_weekly_capacity)) * 100, 1
            ),
            "overbookedSchedulesCount": len(overbooked),
            "overbookedSchedules": overbooked,
        })


# ============================================================
# BOOKING
# ============================================================

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 200


# Fields a patient may change from the public "Check Appointments" page
# (rescheduling). Everything else requires a staff login.
PUBLIC_RESCHEDULE_FIELDS = {"date", "time", "reschedule_reason", "rescheduleReason", "status"}
PUBLIC_RESCHEDULE_STATUSES = {"confirmed"}

LIFECYCLE_STATUSES = {"Pending", "Confirmed", "Checked In", "Completed", "Cancelled"}


class BookingViewSet(viewsets.ModelViewSet):
    """
    Booking API.

    Public (no login): create a booking, availability, public lookup, and
    patient rescheduling (date/time only). Every other read or write is
    restricted to authenticated staff.
    """

    queryset = Booking.objects.all().order_by("-created_at")
    serializer_class = BookingSerializer
    permission_classes = [AllowAny]
    lookup_field = "ref_code"

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.action == "create":
            throttles.append(PublicBookingThrottle())
        elif self.action in ("public_lookup", "duplicate_check", "availability"):
            throttles.append(PublicLookupThrottle())
        return throttles

    def get_serializer_class(self):
        if self.action in ("list", "disabled_bookings"):
            return BookingListSerializer
        return BookingSerializer

    def perform_authentication(self, request):
        # Public endpoints must keep working when a stale token is sent.
        try:
            super().perform_authentication(request)
        except Exception:
            from django.contrib.auth.models import AnonymousUser
            request.user = AnonymousUser()

    def get_queryset(self):
        include_disabled = self.request.query_params.get("include_disabled") == "true"
        queryset = Booking.objects.all()
        if not include_disabled:
            queryset = queryset.filter(is_active=True).exclude(status__iexact="Disabled")
        since = parse_date(self.request.query_params.get("since"))
        if since:
            queryset = queryset.filter(date__gte=since.isoformat())
        if getattr(self, "action", None) == "list":
            queryset = queryset.defer("referral_doc_data", "referral_doc_text")
        return queryset.order_by("-created_at")

    # ---------------------------------------------------------------
    # CRUD
    # ---------------------------------------------------------------

    def perform_create(self, serializer):
        overrides = {}
        if not is_staff_request(self.request):
            # Payment / HMO / lifecycle fields are controlled by the desks,
            # never by the public booking form.
            is_hmo = "hmo" in str(serializer.validated_data.get("payment_type", "")).lower()
            overrides = {
                "status": "Confirmed",
                "is_active": True,
                "payment_status": "HMO Cover" if is_hmo else "Pending",
                "hmo_status": "Pending Pre-Auth" if is_hmo else "N/A",
                "hmo_auth_code": "",
                "invoice_ref": "",
                "delete_reason": "",
                "reminder_sent": False,
            }
            if not is_hmo:
                overrides["hmo_name"] = "N/A"
                overrides["hmo_policy_code"] = ""
        booking = serializer.save(**overrides)
        broadcast_booking_update(
            booking,
            event_type="NEW_BOOKING",
            message=f"New appointment booked for {booking.patient_name} ({booking.ref_code}).",
        )

    def list(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return staff_required_response("view the patient booking registry")

        include_disabled = request.query_params.get("include_disabled") == "true"
        limit_param = request.query_params.get("limit", "")
        cache_key = versioned_key(
            BOOKINGS_LIST_CACHE_KEY, f"inc={include_disabled}", f"lim={limit_param}",
            f"since={request.query_params.get('since', '')}",
        )

        # A per-process cache (no Redis) cannot be invalidated across workers,
        # so it would show stale lists; only cache when the cache is shared.
        use_cache = getattr(settings, "SHARED_CACHE", False)
        cached_data = get_cached_response(cache_key) if use_cache else None
        if cached_data is not None:
            return Response(cached_data)

        response = super().list(request, *args, **kwargs)
        if use_cache and response.status_code == 200 and isinstance(response.data, list):
            set_cached_response(cache_key, response.data, timeout=60)
        return response

    @action(detail=False, methods=["get"], url_path="sync")
    def sync(self, request):
        """
        Incremental booking sync for the dashboard.

        GET /api/bookings/sync/                     -> every active booking
        GET /api/bookings/sync/?date_from=YYYY-MM-DD -> active bookings from that date (fast first paint)
        GET /api/bookings/sync/?since=<server_time>  -> only bookings changed since then;
                                                       refs that left the active list are in "removed"
        Always returns {"server_time", "full", "results", "removed"}.
        """
        if not is_staff_request(request):
            return staff_required_response("view the patient booking registry")

        server_time = timezone.now()
        since_raw = request.query_params.get("since")
        since = None
        if since_raw:
            from django.utils.dateparse import parse_datetime
            since = parse_datetime(since_raw.replace(" ", "+"))
        live = Q(is_active=True) & ~Q(status__iexact="Disabled")

        if since is not None:
            # Small overlap so a change committed during the previous sync is never missed.
            changed = Booking.objects.filter(
                updated_at__gte=since - datetime.timedelta(seconds=5)
            ).defer("referral_doc_data", "referral_doc_text")
            active = [b for b in changed if b.is_active and str(b.status).lower() != "disabled"]
            removed = [b.ref_code for b in changed if not (b.is_active and str(b.status).lower() != "disabled")]
            return Response({
                "server_time": server_time.isoformat(),
                "full": False,
                "results": BookingListSerializer(active, many=True).data,
                "removed": removed,
            })

        qs = Booking.objects.filter(live).defer("referral_doc_data", "referral_doc_text").order_by("-created_at")
        date_from = parse_date(request.query_params.get("date_from"))
        if date_from:
            qs = qs.filter(date__gte=date_from.isoformat())
        return Response({
            "server_time": server_time.isoformat(),
            "full": date_from is None,
            "results": BookingListSerializer(qs, many=True).data,
            "removed": [],
        })

    def retrieve(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return staff_required_response("open booking records")
        return super().retrieve(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Soft-delete: the booking is disabled, never removed."""
        if not is_staff_request(request):
            return staff_required_response("delete or disable appointment records")
        if not is_admin_user(request.user):
            return admin_required_response("delete or disable appointment records")

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
        booking.save(update_fields=["is_active", "status", "delete_reason"])

        broadcast_booking_update(
            booking, event_type="BOOKING_DISABLED",
            message=f"Booking {booking.ref_code} was disabled.",
        )
        return Response({
            "message": f"Booking {booking.ref_code} disabled successfully.",
            "data": BookingSerializer(booking).data,
        })

    @staticmethod
    def _completion_blocked(booking, new_status):
        if (
            str(new_status or "").strip() == "Completed"
            and str(booking.payment_status).strip().lower() == "pending"
        ):
            return Response(
                {
                    "error": (
                        f"Payment Clearance Required: Ticket {booking.ref_code} cannot "
                        "be marked as Completed while payment status is Pending."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def _save_with_serializer(self, request, booking, partial):
        serializer = self.get_serializer(booking, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        booking = serializer.save()
        broadcast_booking_update(
            booking, event_type="BOOKING_UPDATED",
            message=f"Booking {booking.ref_code} updated.",
        )
        return Response(self.get_serializer(booking).data)

    def partial_update(self, request, *args, **kwargs):
        booking = self.get_object()
        keys = set(request.data.keys())
        staff = is_staff_request(request)

        if not staff:
            # Patients may only reschedule (date/time) their own booking.
            new_status = str(request.data.get("status", "Confirmed")).strip().lower()
            if (
                not keys
                or not keys.issubset(PUBLIC_RESCHEDULE_FIELDS)
                or not ({"date", "time"} & keys)
                or new_status not in PUBLIC_RESCHEDULE_STATUSES
            ):
                return staff_required_response("modify this booking")
            if not booking.counts_toward_capacity or booking.status in ("Checked In", "Completed"):
                return Response(
                    {"error": "This appointment can no longer be rescheduled online. Please contact reception."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return self._save_with_serializer(request, booking, partial=True)

        # Desk staff may only change the lifecycle status; editing booking
        # details (patient, date, time, payment fields) is admin-only.
        if keys != {"status"} and not is_admin_user(request.user):
            return admin_required_response("edit booking details")

        blocked = self._completion_blocked(booking, request.data.get("status"))
        if blocked:
            return blocked

        # Fast path for lifecycle-only changes from the desks.
        if keys == {"status"}:
            new_status = str(request.data.get("status", "")).strip()
            if new_status not in LIFECYCLE_STATUSES:
                return Response(
                    {"error": f"Invalid booking status: {new_status}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Moving a cancelled booking back to an active status must go
            # through capacity validation.
            if booking.counts_toward_capacity or new_status == "Cancelled":
                booking.status = new_status
                booking.save(update_fields=["status"])
                broadcast_booking_update(
                    booking, event_type="BOOKING_UPDATED",
                    message=f"Booking {booking.ref_code} updated to {new_status}.",
                )
                return Response(self.get_serializer(booking).data)

        return self._save_with_serializer(request, booking, partial=True)

    def update(self, request, *args, **kwargs):
        if kwargs.get("partial"):
            return self.partial_update(request, *args, **kwargs)
        if not is_staff_request(request):
            return staff_required_response("modify this booking")
        if not is_admin_user(request.user):
            return admin_required_response("edit booking details")
        booking = self.get_object()
        blocked = self._completion_blocked(booking, request.data.get("status"))
        if blocked:
            return blocked
        return self._save_with_serializer(request, booking, partial=False)

    # ---------------------------------------------------------------
    # REMINDERS
    # ---------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="send-reminder")
    def send_reminder(self, request, ref_code=None):
        if not is_staff_request(request):
            return staff_required_response("send reminders")
        booking = self.get_object()
        from api.notification_service import send_single_booking_reminder

        result = send_single_booking_reminder(
            booking, force=parse_bool_status(request.data.get("force"), default=False)
        )
        broadcast_booking_update(
            booking, event_type="REMINDER_SENT",
            message=f"Reminder sent to {booking.patient_name} ({booking.ref_code}).",
        )
        return Response(result)

    @action(detail=False, methods=["post"], url_path="send-bulk-reminders")
    def send_bulk_reminders(self, request):
        if not is_staff_request(request):
            return staff_required_response("send reminders")
        from api.notification_service import process_appointment_reminders

        summary = process_appointment_reminders(
            target_date=request.data.get("target_date") or request.data.get("date") or None,
            days_ahead=safe_int(request.data.get("days_ahead"), 1, 0),
            force=parse_bool_status(request.data.get("force"), default=False),
        )
        return Response(summary)

    # ---------------------------------------------------------------
    # DASHBOARD
    # ---------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        if not is_staff_request(request):
            return staff_required_response("view booking statistics")

        cache_key = versioned_key(BOOKING_SUMMARY_CACHE_KEY)
        cached = get_cached_response(cache_key)
        if cached is not None:
            return Response(cached)

        live = Booking.objects.filter(is_active=True).exclude(status__iexact="Disabled")
        today_iso = timezone.localdate().isoformat()
        summary = live.aggregate(
            total=Count("ref_code"),
            today=Count("ref_code", filter=Q(date=today_iso)),
            checked_in=Count("ref_code", filter=Q(status="Checked In", date=today_iso)),
            pending_hmo=Count(
                "ref_code",
                filter=Q(payment_type="HMO Insurance") & ~Q(hmo_status__in=["Approved", "Declined"]),
            ),
            pending_cash=Count(
                "ref_code",
                filter=Q(payment_type="Private Self-Pay") & ~Q(payment_status="Cleared"),
            ),
            declined_hmo=Count(
                "ref_code",
                filter=Q(payment_type="HMO Insurance") & Q(hmo_status="Declined"),
            ),
        )
        payload = {
            "totalBookings": summary.get("total") or 0,
            "checkedInCount": summary.get("checked_in") or 0,
            "todayCount": summary.get("today") or 0,
            "date": today_iso,
            "pendingHmoCount": summary.get("pending_hmo") or 0,
            "pendingCashCount": summary.get("pending_cash") or 0,
            "declinedHmoCount": summary.get("declined_hmo") or 0,
        }
        set_cached_response(cache_key, payload)
        return Response(payload)

    @action(detail=False, methods=["post"], url_path="clear-all")
    def clear_all(self, request):
        if not is_staff_request(request):
            return staff_required_response("clear bookings")
        if not is_admin_user(request.user):
            return admin_required_response("clear bookings")

        reason = request.data.get("reason") or "Cleared by authorized administrator"
        count = Booking.objects.filter(is_active=True).update(
            is_active=False, status="Disabled", delete_reason=reason, updated_at=timezone.now(),
        )
        broadcast_bulk_refresh(
            action_name="BOOKINGS_CLEARED",
            message=f"{count} booking records were disabled by administrator.",
        )
        return Response({"message": f"{count} booking records disabled.", "count": count})

    # ---------------------------------------------------------------
    # PUBLIC
    # ---------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="availability", permission_classes=[AllowAny])
    def availability(self, request):
        doctor_id = str(request.query_params.get("doctor_id") or "").strip()
        date_str = str(request.query_params.get("date") or "").strip()
        if not doctor_id or not date_str:
            return Response(
                {"error": "doctor_id and date are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        appointment_date = parse_date(date_str)
        if not appointment_date:
            return Response(
                {"error": "Invalid appointment date. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        doctor = (
            Doctor.objects.filter(doc_id__iexact=doctor_id)
            .prefetch_related("schedules", "schedule_exceptions").first()
        )
        if not doctor:
            return Response({"error": "Doctor not found."}, status=status.HTTP_404_NOT_FOUND)

        resolved = resolve_doctor_day(doctor, appointment_date)
        on_duty = resolved["on_duty"]
        capacity = resolved["capacity"] if on_duty else 0
        booked = count_doctor_bookings(doctor.doc_id, appointment_date)
        is_full = on_duty and booked >= capacity
        is_past = appointment_date < timezone.localdate()

        return Response({
            "doctorId": doctor.doc_id,
            "date": appointment_date.isoformat(),
            "booked": booked,
            "capacity": capacity,
            "remaining": max(0, capacity - booked),
            "is_full": is_full,
            "available": on_duty and not is_full and not is_past and bool(doctor.status),
            "onDuty": on_duty,
            "timeWindow": resolved["shift"],
            "note": resolved["note"],
        })

    @action(detail=False, methods=["get"], url_path="duplicate-check", permission_classes=[AllowAny])
    def duplicate_check(self, request):
        """
        Lets the booking form warn a patient before they submit. Requires the
        full name AND phone, and reveals no more than the public lookup does.
        """
        from .duplicates import duplicate_message, find_duplicate_booking

        doctor = Doctor.objects.filter(
            doc_id__iexact=str(request.query_params.get("doctor_id") or "").strip()
        ).select_related("department").first()
        if not doctor:
            return Response({"duplicate": False})
        existing = find_duplicate_booking(
            doctor,
            request.query_params.get("patient_name"),
            request.query_params.get("patient_phone"),
            exclude_ref=request.query_params.get("exclude_ref") or None,
        )
        if not existing:
            return Response({"duplicate": False})
        return Response({
            "duplicate": True,
            "clinic": existing.clinic_label,
            "date": existing.date,
            "time": existing.time,
            # Masked: the full reference alone allows rescheduling.
            "refCode": mask_ref(existing.ref_code),
            "message": duplicate_message(existing, existing.clinic_label),
        })

    @action(detail=False, methods=["get"], url_path="public-lookup", permission_classes=[AllowAny])
    def public_lookup(self, request):
        ref_code = str(request.query_params.get("ref_code") or "").strip()
        phone = str(request.query_params.get("phone") or "").strip()

        if not ref_code and not phone:
            return Response(
                {"error": "Booking reference or phone number is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Disabled (trashed) bookings exist only in Archive & Trash.
        live = Booking.objects.filter(is_active=True).exclude(status__iexact="Disabled")
        if ref_code:
            booking = live.filter(ref_code__iexact=ref_code).first()
        else:
            booking = (
                live.filter(patient_phone__iexact=phone)
                .order_by("-created_at").first()
            )

        if not booking or (phone and booking.patient_phone.strip() != phone):
            return Response({"error": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)

        # Patients see their appointment, never uploaded documents, email or
        # insurance codes (anyone who knows a phone number can call this).
        return Response(public_booking_payload(booking))

    # ---------------------------------------------------------------
    # ARCHIVE
    # ---------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="disabled")
    def disabled_bookings(self, request):
        if not is_staff_request(request):
            return staff_required_response("view archived bookings")
        queryset = (
            Booking.objects.filter(Q(is_active=False) | Q(status__iexact="Disabled"))
            .defer("referral_doc_data", "referral_doc_text")
            .order_by("-created_at")
        )
        return Response(BookingListSerializer(queryset, many=True).data)

    @action(detail=True, methods=["post"], url_path="restore")
    def restore_booking(self, request, ref_code=None):
        if not is_staff_request(request):
            return staff_required_response("restore bookings")
        if not is_admin_user(request.user):
            return admin_required_response("restore bookings")

        booking = Booking.objects.filter(ref_code=ref_code).first()
        if not booking:
            return Response({"error": "Booking record not found."}, status=status.HTTP_404_NOT_FOUND)

        # A restored booking consumes capacity again.
        doctor = Doctor.objects.filter(doc_id=booking.doctor_id).prefetch_related("schedules").first()
        if doctor:
            resolved = resolve_doctor_day(doctor, booking.date)
            capacity = resolved["capacity"] if resolved["on_duty"] else 0
            booked = count_doctor_bookings(doctor.doc_id, booking.date, exclude_ref=booking.ref_code)
            if resolved["on_duty"] and booked >= capacity:
                return Response(
                    {
                        "error": (
                            f"Cannot restore {booking.ref_code}: {get_doctor_name(doctor)} "
                            f"is already fully booked ({booked}/{capacity}) on {booking.date}."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Restoring must not create a double booking for the same patient.
        if doctor:
            from .duplicates import duplicate_message, find_duplicate_booking
            existing = find_duplicate_booking(
                doctor, booking.patient_name, booking.patient_phone, exclude_ref=booking.ref_code
            )
            if existing is not None:
                return Response(
                    {
                        "error": "Cannot restore: " + duplicate_message(existing, existing.clinic_label),
                        "duplicate": True,
                        "existing_ref": existing.ref_code,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        booking.is_active = True
        booking.status = "Confirmed"
        booking.delete_reason = ""
        booking.save(update_fields=["is_active", "status", "delete_reason"])

        broadcast_booking_update(
            booking, event_type="BOOKING_RESTORED",
            message=f"Booking {booking.ref_code} restored successfully.",
        )
        return Response({
            "message": f"Booking {booking.ref_code} restored successfully.",
            "data": BookingSerializer(booking).data,
        })

    # ---------------------------------------------------------------
    # DESK ACTIONS (staff only)
    # ---------------------------------------------------------------

    @action(detail=True, methods=["post", "patch"], url_path="reroute-cashdesk")
    def reroute_cashdesk(self, request, ref_code=None):
        if not is_staff_request(request):
            return staff_required_response("reroute bookings")

        booking = Booking.objects.filter(ref_code=ref_code).first()
        if not booking:
            return Response(
                {"error": f"Booking ticket {ref_code} not found."},
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
        booking.hmo_status = f"Re-routed to Cashdesk (Self-Pay): {remark}"
        booking.payment_status = "Pending"
        booking.delete_reason = f"Re-routed from HMO to Cashdesk: {remark}"
        booking.save(update_fields=[
            "payment_type", "hmo_name", "hmo_status", "payment_status", "delete_reason",
        ])

        broadcast_booking_update(
            booking, event_type="BOOKING_REROUTED_CASHDESK",
            message=f"Ticket {booking.ref_code} re-routed to Cashdesk.",
        )
        return Response({
            "message": f"Ticket {booking.ref_code} re-routed to Cashdesk as Private Self-Pay.",
            "data": BookingSerializer(booking).data,
        })

    @action(detail=True, methods=["post"], url_path="check-in")
    def check_in(self, request, ref_code=None):
        if not is_staff_request(request):
            return staff_required_response("check patients in")

        booking = self.get_object()
        if booking.payment_type == "HMO Insurance" and booking.hmo_status != "Approved":
            return Response(
                {
                    "error": (
                        f"HMO Approval Required: Cannot check in ticket {booking.ref_code} "
                        f"while HMO status is {booking.hmo_status or 'Awaiting Approval'}. "
                        "Route patient to HMO Desk first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if booking.payment_status == "Pending":
            return Response(
                {
                    "error": (
                        f"Payment Clearance Required: Cannot check in ticket {booking.ref_code} "
                        "while payment is Pending. Route patient to Cashdesk first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        booking.status = "Checked In"
        booking.save(update_fields=["status"])
        broadcast_booking_update(
            booking, event_type="PATIENT_CHECKED_IN",
            message=f"Patient {booking.patient_name} ({booking.ref_code}) checked in.",
        )
        return Response({
            "message": f"Patient {booking.patient_name} checked in successfully.",
            "data": BookingSerializer(booking).data,
        })

    @action(detail=True, methods=["post"], url_path="approve-hmo")
    def approve_hmo(self, request, ref_code=None):
        if not is_staff_request(request):
            return staff_required_response("approve HMO authorizations")

        booking = self.get_object()
        policy = (
            str(request.data.get("policyCode") or "").strip()
            or booking.hmo_policy_code
            or f"POL-{random.randint(100000, 999999)}"
        )
        auth = str(request.data.get("authCode") or "").strip() or booking.hmo_auth_code
        if not auth:
            return Response(
                {"error": "An HMO authorization code is required to approve this booking."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        booking.hmo_policy_code = policy
        booking.hmo_auth_code = auth
        booking.hmo_status = "Approved"
        booking.payment_status = "Cleared"
        # Approving a previously declined request clears the decline record.
        booking.hmo_decline_reason = ""
        booking.hmo_declined_at = None
        booking.hmo_declined_by = ""
        booking.save(update_fields=[
            "hmo_policy_code", "hmo_auth_code", "hmo_status", "payment_status",
            "hmo_decline_reason", "hmo_declined_at", "hmo_declined_by",
        ])

        broadcast_booking_update(
            booking, event_type="HMO_APPROVED",
            message=f"HMO pre-authorization approved for ticket {booking.ref_code}.",
        )
        return Response({
            "message": f"Pre-Authorization cleared for ticket {booking.ref_code}.",
            "authCode": auth,
            "data": BookingSerializer(booking).data,
        })

    @action(detail=True, methods=["post"], url_path="decline-hmo")
    def decline_hmo(self, request, ref_code=None):
        """HMO desk refuses pre-authorization: the booking moves to Declined HMO Approvals."""
        if not is_staff_request(request):
            return staff_required_response("decline HMO authorizations")

        booking = self.get_object()
        if str(booking.payment_type or "").strip().lower() != "hmo insurance":
            return Response(
                {"error": f"Ticket {booking.ref_code} is not an HMO booking."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if str(booking.hmo_status or "").strip().lower() == "approved":
            return Response(
                {"error": f"Ticket {booking.ref_code} is already approved and cannot be declined."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = str(request.data.get("reason") or "").strip()[:1000] or "Declined by HMO desk"
        user = request.user
        actor = (
            (getattr(user, "first_name", "") or "").strip()
            or (user.get_full_name() if hasattr(user, "get_full_name") else "")
            or getattr(user, "username", "")
            or "HMO Desk"
        )
        booking.hmo_status = "Declined"
        booking.hmo_decline_reason = reason
        booking.hmo_declined_at = timezone.now()
        booking.hmo_declined_by = str(actor)[:200]
        booking.save(update_fields=[
            "hmo_status", "hmo_decline_reason", "hmo_declined_at", "hmo_declined_by",
        ])

        broadcast_booking_update(
            booking, event_type="HMO_DECLINED",
            message=f"HMO pre-authorization declined for ticket {booking.ref_code}.",
        )
        return Response({
            "message": f"HMO pre-authorization declined for ticket {booking.ref_code}.",
            "data": BookingSerializer(booking).data,
        })

    @action(detail=True, methods=["post"], url_path="reopen-hmo")
    def reopen_hmo(self, request, ref_code=None):
        """Send a declined booking back to the HMO approval queue."""
        if not is_staff_request(request):
            return staff_required_response("re-open HMO authorizations")

        booking = self.get_object()
        if str(booking.hmo_status or "").strip().lower() != "declined":
            return Response(
                {"error": f"Ticket {booking.ref_code} is not in Declined HMO Approvals."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        booking.hmo_status = "Awaiting Approval"
        booking.hmo_decline_reason = ""
        booking.hmo_declined_at = None
        booking.hmo_declined_by = ""
        booking.save(update_fields=[
            "hmo_status", "hmo_decline_reason", "hmo_declined_at", "hmo_declined_by",
        ])
        broadcast_booking_update(
            booking, event_type="HMO_REOPENED",
            message=f"Ticket {booking.ref_code} returned to the HMO approval queue.",
        )
        return Response({
            "message": f"Ticket {booking.ref_code} is back in the HMO approval queue.",
            "data": BookingSerializer(booking).data,
        })

    @action(detail=True, methods=["post"], url_path="pay-cashdesk")
    def pay_cashdesk(self, request, ref_code=None):
        if not is_staff_request(request):
            return staff_required_response("clear payments")

        booking = self.get_object()
        if str(booking.payment_status).lower() == "cleared":
            return Response({
                "message": f"Payment for {booking.ref_code} was already cleared.",
                "invoiceRef": booking.invoice_ref,
                "data": BookingSerializer(booking).data,
            })

        method = request.data.get("paymentMethod") or "POS Card Terminal"
        invoice = f"INV-{random.randint(100000, 999999)}"
        booking.payment_status = "Cleared"
        booking.payment_method = method
        booking.invoice_ref = invoice
        booking.save(update_fields=["payment_status", "payment_method", "invoice_ref"])

        broadcast_booking_update(
            booking, event_type="PAYMENT_CLEARED",
            message=f"Cashdesk payment cleared via {method} for ticket {booking.ref_code}.",
        )
        return Response({
            "message": f"Cashdesk payment cleared via {method}.",
            "invoiceRef": invoice,
            "data": BookingSerializer(booking).data,
        })


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
    permission_classes = [StaffCreateAdminChange]
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
        User.objects.all()
        .select_related("profile", "profile__role")
        .order_by("-date_joined", "-id")
    )
    serializer_class = SystemUserSerializer
    permission_classes = [StaffReadAdminWrite]
    lookup_field = "id"

    def get_object(self):
        value = str(self.kwargs[self.lookup_url_kwarg or self.lookup_field])
        if value.startswith("usr-"):
            value = value[4:]
        try:
            user = User.objects.get(id=int(value))
        except (User.DoesNotExist, ValueError):
            raise Http404("System user not found.")
        self.check_object_permissions(self.request, user)
        return user

    def create(self, request, *args, **kwargs):
        """
        Create a staff account. A password is mandatory, and an existing
        account is never overwritten (the old behaviour silently reset the
        password of whoever already owned that email).
        """
        email = str(request.data.get("email") or "").strip().lower()
        if email and User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exists():
            return Response(
                {"error": f"A staff account with email '{email}' already exists. Edit that account instead."},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return error_response(serializer.errors)
        with transaction.atomic():
            user = serializer.save()
        return Response(self.get_serializer(user).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        user = self.get_object()
        if (
            user.pk == request.user.pk
            and "status" in request.data
            and not parse_bool_status(request.data.get("status"), default=True)
        ):
            return Response(
                {"error": "You cannot disable your own account while signed in."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = self.get_serializer(user, data=request.data, partial=partial)
        if not serializer.is_valid():
            return error_response(serializer.errors)
        with transaction.atomic():
            user = serializer.save()
        return Response(self.get_serializer(user).data)

    def destroy(self, request, *args, **kwargs):
        """Deactivate (never hard-delete) a staff account."""
        user = self.get_object()
        if user.pk == request.user.pk:
            return Response(
                {"error": "You cannot deactivate your own account while signed in."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.is_active = False
        user.save(update_fields=["is_active"])
        return Response({"message": f"User {user.username} deactivated successfully."})


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
    permission_classes = [AdminOrReadOnly]
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
        raw_prompt = request.data.get("prompt")
        if isinstance(raw_prompt, dict):
            raw_prompt = raw_prompt.get("prompt")
        prompt = str(raw_prompt or "Generate Full Executive Board Report").strip()[:500]

        active = capacity_bookings_qs()

        total = active.count()
        checked_in = active.filter(status="Checked In").count()
        completed = active.filter(status="Completed").count()
        pending_hmo = (
            active
            .filter(payment_type="HMO Insurance")
            .exclude(hmo_status__in=["Approved", "Declined"])
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


# ============================================================
# SCHEDULE EXCEPTIONS (cancel / move one clinic date)
# ============================================================

class ScheduleExceptionViewSet(viewsets.ViewSet):
    """
    GET    /api/schedule-exceptions/?doctor=doc-1&upcoming=true
    GET    /api/schedule-exceptions/preview/?doctor_id=doc-1&date=2026-11-07
    POST   /api/schedule-exceptions/        {doctor_id, original_date, action, new_date?, shift_time?, reason}
    GET    /api/schedule-exceptions/<id>/   (poll notification progress)
    POST   /api/schedule-exceptions/<id>/retry-notifications/
    DELETE /api/schedule-exceptions/<id>/   (undo; only if no bookings were affected)
    """

    permission_classes = [StaffReadAdminWrite]

    def _get(self, pk):
        exc = ScheduleException.objects.select_related("doctor", "doctor__department").filter(exception_id=pk).first()
        if not exc:
            raise Http404("Schedule change not found.")
        return exc

    def list(self, request):
        qs = ScheduleException.objects.select_related("doctor", "doctor__department")
        doctor = request.query_params.get("doctor") or request.query_params.get("doctor_id")
        if doctor:
            qs = qs.filter(doctor__doc_id__iexact=doctor.strip())
        if request.query_params.get("upcoming", "true") != "false":
            today = timezone.localdate().isoformat()
            qs = qs.filter(Q(original_date__gte=today) | Q(new_date__gte=today))
        return Response(ScheduleExceptionSerializer(qs.order_by("original_date"), many=True).data)

    def retrieve(self, request, pk=None):
        return Response(ScheduleExceptionSerializer(self._get(pk)).data)

    @action(detail=False, methods=["get"], url_path="preview")
    def preview(self, request):
        doctor = (
            Doctor.objects.filter(doc_id__iexact=str(request.query_params.get("doctor_id") or "").strip())
            .prefetch_related("schedules", "schedule_exceptions").first()
        )
        if not doctor:
            return Response({"error": "Doctor not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            return Response(preview_schedule_change(doctor, request.query_params.get("date")))
        except ScheduleChangeError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request):
        data = request.data
        shift = data.get("shift_times") or data.get("shiftTimes") or data.get("shift_time") or data.get("shiftTime")
        user = request.user
        try:
            exception = apply_schedule_exception(
                doctor_id=data.get("doctor_id") or data.get("doctorId"),
                original_date=data.get("original_date") or data.get("originalDate"),
                action=str(data.get("action") or "").strip().lower(),
                new_date=data.get("new_date") or data.get("newDate"),
                shift_times=shift,
                reason=data.get("reason"),
                created_by=(user.get_full_name() or user.first_name or user.username) if user and user.is_authenticated else "",
            )
        except ScheduleChangeError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        broadcast_bulk_refresh(
            action_name="SCHEDULE_CHANGED",
            message=(
                f"Clinic on {exception.original_date} "
                + ("cancelled." if exception.action == "cancel" else f"moved to {exception.new_date}.")
            ),
        )
        return Response(
            ScheduleExceptionSerializer(self._get(exception.exception_id)).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path="channels")
    def channels(self, request):
        """Whether email/SMS will really be delivered (no secrets returned)."""
        from .notification_service import email_backend_status, sms_gateway_status
        return Response({"email": email_backend_status(), "sms": sms_gateway_status()})

    @action(detail=False, methods=["post"], url_path="test-notification")
    def test_notification(self, request):
        """Admin: send a real test email and/or SMS to confirm delivery."""
        from .notification_service import send_test_notification
        email = str(request.data.get("email") or "").strip()
        phone = str(request.data.get("phone") or "").strip()
        if not email and not phone:
            return Response({"error": "Enter an email address and/or phone number."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(send_test_notification(email=email, phone=phone))

    @action(detail=True, methods=["post"], url_path="retry-notifications")
    def retry_notifications(self, request, pk=None):
        exc = self._get(pk)
        count = retry_failed_notifications(exc)
        return Response({"retrying": count, "data": ScheduleExceptionSerializer(self._get(pk)).data})

    def destroy(self, request, pk=None):
        exc = self._get(pk)
        try:
            undo_schedule_exception(exc)
        except ScheduleChangeError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        broadcast_bulk_refresh(action_name="SCHEDULE_CHANGED", message="Schedule change undone.")
        return Response({"message": "Schedule change undone."})


# ============================================================
# CLINIC SESSIONS ANALYTICS
# ============================================================

class ClinicAnalyticsViewSet(viewsets.ViewSet):
    """
    Real-time data for the Clinic Sessions banner on the public booking page.
    """
    permission_classes = [AllowAny]

    def list(self, request):
        today = timezone.localdate()
        today_str = today.isoformat()

        today_bookings = list(
            capacity_bookings_qs()
            .filter(date=today_str)
            .values("doctor_id", "doctor_specialty")
        )

        doctors = list(
            Doctor.objects.filter(status=True)
            .select_related("department")
            .prefetch_related("schedules", "schedule_exceptions")
        )
        doctor_dept = {d.doc_id: d.department_id for d in doctors}

        # Count today's patients per department (by doctor first, then by the
        # specialty text stored on the booking). Booking has no department
        # field; the old code read b.department and crashed.
        dept_by_name = {
            d.name.strip().lower(): d.dept_id
            for d in Department.objects.filter(status=True)
        }
        booking_count_map = {}
        for b in today_bookings:
            dept_id = doctor_dept.get(b["doctor_id"]) or dept_by_name.get(
                str(b["doctor_specialty"] or "").strip().lower()
            )
            if dept_id:
                booking_count_map[dept_id] = booking_count_map.get(dept_id, 0) + 1

        runs_today = set()
        for doc in doctors:
            if doc.department_id and doc.department_id not in runs_today:
                if resolve_doctor_day(doc, today)["on_duty"]:
                    runs_today.add(doc.department_id)

        active_clinics_list = []
        for dept in Department.objects.filter(status=True).order_by("name"):
            patient_count = booking_count_map.get(dept.dept_id, 0)
            runs = dept.dept_id in runs_today
            if runs or patient_count:
                active_clinics_list.append({
                    "dept_id": dept.dept_id,
                    "clinicName": dept.name.strip(),
                    "patientCount": patient_count,
                    "runsToday": runs,
                    "hasBookings": patient_count > 0,
                })

        return Response({
            "date": today_str,
            "dayFormatted": today.strftime("%A, %b %d"),
            "totalActiveClinics": len(active_clinics_list),
            "totalPatientAppointmentsToday": len(today_bookings),
            "clinics": active_clinics_list,
        })
