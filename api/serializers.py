import random
import re
import time
import uuid

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers

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
    UserProfile,
)
from .duplicates import duplicate_message, find_duplicate_booking
from .scheduling import (
    ScheduleFormatError,
    canonicalize_schedule,
    is_weekday_only,
    capacity_bookings_qs,
    compute_weekly_capacity,
    count_doctor_bookings,
    describe_rule_errors,
    normalize_day_configs,
    normalize_duty_days,
    parse_date,
    parse_time_of_day,
    resolve_doctor_day,
    safe_int,
    week_of_month,
    INACTIVE_BOOKING_STATUSES,
)


# ============================================================
# COMMON HELPERS
# ============================================================

_TRUE_VALUES = {
    "true", "1", "yes", "y", "on", "active", "enabled", "enable",
    "active partner", "active on duty", "on duty", "confirmed",
}
_FALSE_VALUES = {
    "false", "0", "no", "n", "off", "inactive", "disabled", "disable",
    "off duty", "cancelled", "maintenance", "under maintenance", "suspended",
}


def parse_bool_status(val, default=True):
    """
    Convert frontend status values ("Active", "Disabled Shift 🚫",
    "Active On Duty", "Maintenance", true, 0 ...) into booleans.
    """
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0

    s = str(val).strip().lower()
    if s in _TRUE_VALUES:
        return True
    if s in _FALSE_VALUES:
        return False

    # Decorated labels such as "Disabled Shift 🚫" or "Active On Duty ✓".
    if re.search(r"\b(disabled?|inactive|suspend\w*|maintenance|off)\b", s):
        return False
    if re.search(r"\b(active|enabled?|on duty)\b", s):
        return True
    return default


def _mutable_copy(data):
    if hasattr(data, "dict"):
        # QueryDict -> plain dict (keeps the last value of each key)
        return data.dict()
    if hasattr(data, "copy"):
        return data.copy()
    return dict(data)


def _apply_aliases(data, mapping):
    """Copy camelCase keys onto snake_case keys when the latter are empty."""
    for camel, snake in mapping.items():
        if camel in data and (snake not in data or data[snake] in (None, "", [], {})):
            data[snake] = data[camel]
    return data


def _generate_id(prefix):
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(100, 999)}"


def _resolve_doctor(value):
    """Find a Doctor by doc_id (case-insensitive) or display name."""
    if value in (None, ""):
        return None
    if isinstance(value, Doctor):
        return value
    if isinstance(value, dict):
        value = value.get("doc_id") or value.get("id") or value.get("name") or ""
    key = str(value).strip()
    if not key:
        return None
    return (
        Doctor.objects.filter(doc_id__iexact=key).first()
        or Doctor.objects.filter(full_name__iexact=key).first()
        or Doctor.objects.filter(name__iexact=key).first()
    )


# ============================================================
# DEPARTMENT SERIALIZER
# ============================================================

class DepartmentSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="dept_id", read_only=True)
    dept_id = serializers.CharField(required=False, max_length=50)

    class Meta:
        model = Department
        fields = "__all__"

    def to_internal_value(self, data):
        data_copy = _mutable_copy(data)
        _apply_aliases(data_copy, {
            "departmentId": "dept_id",
            "department_id": "dept_id",
            "iconName": "icon_name",
            "doctorCount": "doctor_count",
        })

        if self.instance:
            # The primary key can never be changed through the API.
            data_copy["dept_id"] = self.instance.dept_id
        elif not data_copy.get("dept_id"):
            data_copy["dept_id"] = data_copy.get("id") or ""

        if not self.instance and not str(data_copy.get("dept_id") or "").strip():
            base = slugify(str(data_copy.get("name") or ""))[:40] or "dept"
            candidate = base
            n = 2
            while Department.objects.filter(dept_id=candidate).exists():
                candidate = f"{base}-{n}"
                n += 1
            data_copy["dept_id"] = candidate

        if "status" in data_copy:
            data_copy["status"] = parse_bool_status(data_copy["status"])

        return super().to_internal_value(data_copy)

    def validate_dept_id(self, value):
        value = str(value).strip()
        if not value:
            raise serializers.ValidationError("Department ID is required.")
        if (
            not self.instance
            and Department.objects.filter(dept_id__iexact=value).exists()
        ):
            raise serializers.ValidationError(
                f"A department with ID '{value}' already exists."
            )
        return value

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["id"] = instance.dept_id
        ret["dept_id"] = instance.dept_id
        ret["status"] = instance.status
        ret["location"] = getattr(instance, "location", None) or "Main Building"
        ret["iconName"] = instance.icon_name

        doc_count = instance.doctors.count()
        ret["doctor_count"] = doc_count if doc_count > 0 else (instance.doctor_count or 0)
        ret["doctorCount"] = ret["doctor_count"]
        return ret


# ============================================================
# DOCTOR SERIALIZER
# ============================================================

class DoctorSerializer(serializers.ModelSerializer):
    doc_id = serializers.CharField(required=False)
    active_booking_count = serializers.SerializerMethodField()
    name = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Doctor
        fields = "__all__"

    def get_active_booking_count(self, obj):
        """Booking.doctor_id stores Doctor.doc_id."""
        try:
            return capacity_bookings_qs().filter(doctor_id=obj.doc_id).count()
        except Exception:
            return 0

    def to_internal_value(self, data):
        data_copy = _mutable_copy(data)

        if self.instance:
            data_copy["doc_id"] = self.instance.doc_id
        else:
            data_copy["doc_id"] = (
                data_copy.get("doc_id") or data_copy.get("id") or _generate_id("doc")
            )

        if not data_copy.get("name") and not self.instance:
            data_copy["name"] = (
                data_copy.get("fullName") or data_copy.get("full_name") or "Doctor"
            )

        # Only real model fields are mapped. Room / days / time slots belong
        # to SpecialistSchedule and are handled by DoctorViewSet.
        _apply_aliases(data_copy, {
            "fullName": "full_name",
            "acceptedPatientTypes": "accepted_patient_types",
        })

        dept_val = (
            data_copy.get("department_id")
            or data_copy.get("departmentId")
            or data_copy.get("department")
        )
        if dept_val:
            if isinstance(dept_val, dict):
                dept_str = str(
                    dept_val.get("dept_id") or dept_val.get("id") or dept_val.get("name") or ""
                ).strip()
            else:
                dept_str = str(dept_val).strip()

            dept_obj = None
            if dept_str:
                dept_obj = (
                    Department.objects.filter(dept_id__iexact=dept_str).first()
                    or Department.objects.filter(name__iexact=dept_str).first()
                    or Department.objects.filter(name__icontains=dept_str).first()
                )
            if dept_obj:
                data_copy["department"] = dept_obj.dept_id
            elif self.instance and self.instance.department:
                data_copy["department"] = self.instance.department.dept_id
            else:
                data_copy["department"] = None
        elif self.instance and self.instance.department:
            data_copy["department"] = self.instance.department.dept_id

        if "status" in data_copy:
            data_copy["status"] = parse_bool_status(data_copy["status"])

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)

        ret["id"] = instance.doc_id
        ret["doc_id"] = instance.doc_id
        ret["fullName"] = instance.full_name or instance.name
        ret["full_name"] = instance.full_name or instance.name

        ret["departmentId"] = instance.department.dept_id if instance.department else ""
        ret["department_id"] = ret["departmentId"]
        if instance.department:
            ret["department"] = {
                "dept_id": instance.department.dept_id,
                "id": instance.department.dept_id,
                "name": instance.department.name,
                "description": instance.department.description,
                "icon_name": instance.department.icon_name,
            }
        else:
            ret["department"] = None

        ret["availableDays"] = instance.available_days or []
        ret["availability"] = instance.available_days or []
        ret["timeSlots"] = instance.time_slots or []
        ret["roomNumber"] = instance.room_number or ""
        ret["room"] = instance.room_number or ""

        types = instance.accepted_patient_types or ["Private Self-Pay", "HMO Insurance"]
        ret["acceptedPatientTypes"] = types
        ret["accepted_patient_types"] = types

        ret["status"] = instance.status

        ret["active_booking_count"] = self.get_active_booking_count(instance)
        ret["activeBookingCount"] = ret["active_booking_count"]

        cap = instance.daily_capacity
        ret["capacity"] = cap
        ret["daily_capacity"] = cap
        ret["dailyCapacity"] = cap
        ret["maxDailyAppointments"] = cap

        schedule = instance.active_schedule
        if schedule:
            total_cap = schedule.total_weekly_capacity or compute_weekly_capacity(
                schedule.duty_days, schedule.day_configs, schedule.capacity
            )
            ret["total_weekly_capacity"] = total_cap
            ret["totalWeeklyCapacity"] = total_cap
            ret["day_configs"] = schedule.day_configs or {}
            ret["dayConfigs"] = schedule.day_configs or {}
            ret["shift_time"] = schedule.shift_time
            ret["shiftTime"] = schedule.shift_time

        return ret


# ============================================================
# SPECIALIST SCHEDULE SERIALIZER
# ============================================================

class SpecialistScheduleSerializer(serializers.ModelSerializer):
    sched_id = serializers.CharField(required=False)

    class Meta:
        model = SpecialistSchedule
        fields = "__all__"
        # Always derived from duty days / per-day capacities.
        read_only_fields = ["total_weekly_capacity"]

    def to_internal_value(self, data):
        data_copy = _mutable_copy(data)

        if self.instance:
            data_copy["sched_id"] = self.instance.sched_id
        else:
            data_copy["sched_id"] = (
                data_copy.get("sched_id") or data_copy.get("id") or _generate_id("sched")
            )

        _apply_aliases(data_copy, {
            "doctorName": "doctor_name",
            "dutyDays": "duty_days",
            "availableDays": "duty_days",
            "dayConfigs": "day_configs",
            "shiftTime": "shift_time",
            "roomNumber": "room",
            "room_number": "room",
            "dailyCapacity": "capacity",
            "maxDailyAppointments": "capacity",
        })

        if "duty_days" in data_copy:
            data_copy["duty_days"] = normalize_duty_days(data_copy["duty_days"])
        if "day_configs" in data_copy:
            data_copy["day_configs"] = normalize_day_configs(data_copy["day_configs"])
        if "capacity" in data_copy:
            data_copy["capacity"] = safe_int(data_copy["capacity"], 15, 0)
        if "status" in data_copy:
            data_copy["status"] = parse_bool_status(data_copy["status"])

        # ---- doctor --------------------------------------------------
        doc_keys = ("doctor_id", "doctorId", "doctor")
        doc_val = next(
            (data_copy.get(k) for k in doc_keys if data_copy.get(k) not in (None, "")),
            None,
        )
        doctor_given = any(k in data_copy for k in doc_keys)

        if doc_val is None and not self.instance and data_copy.get("doctor_name"):
            # Legacy clients that only send a name.
            doc_val = re.sub(r"\s*\(.*\)\s*$", "", str(data_copy["doctor_name"]))

        if doc_val is not None:
            doc_obj = _resolve_doctor(doc_val)
            if not doc_obj:
                raise serializers.ValidationError({
                    "doctor": (
                        "The selected doctor does not exist in the hospital "
                        "database. Register the doctor first, then assign a schedule."
                    )
                })
            data_copy["doctor"] = doc_obj.doc_id
        elif doctor_given and self.instance and self.instance.doctor:
            data_copy["doctor"] = self.instance.doctor.doc_id
        elif self.instance and self.instance.doctor:
            data_copy["doctor"] = self.instance.doctor.doc_id

        return super().to_internal_value(data_copy)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance

        doctor = attrs.get("doctor", getattr(instance, "doctor", None))
        if not doctor:
            raise serializers.ValidationError({
                "doctor": "A registered specialist doctor is required for every schedule."
            })

        room = attrs.get("room", getattr(instance, "room", ""))
        if not str(room or "").strip():
            raise serializers.ValidationError({"room": "Consultation room / suite is required."})

        duty_days = attrs.get("duty_days", getattr(instance, "duty_days", []))
        day_configs = attrs.get("day_configs", getattr(instance, "day_configs", {}))
        schedule_changed = instance is None or "duty_days" in attrs or "day_configs" in attrs

        # New schedules, and any edit of weekday-based schedules, are stored in
        # the canonical JSON format (see api.scheduling.canonicalize_schedule).
        # Older rows that still use labels such as "📅 1ST & 3RD SUNDAYS" keep
        # working and can be edited without being rejected.
        if schedule_changed and (instance is None or is_weekday_only(duty_days, day_configs)):
            try:
                duty_days, day_configs = canonicalize_schedule(
                    duty_days,
                    day_configs,
                    attrs.get("capacity", getattr(instance, "capacity", 15)),
                    attrs.get("shift_time", getattr(instance, "shift_time", "")),
                )
            except ScheduleFormatError as exc:
                raise serializers.ValidationError({"day_configs": str(exc)})
            attrs["duty_days"] = duty_days
            attrs["day_configs"] = day_configs

        if not duty_days:
            raise serializers.ValidationError({
                "duty_days": "Select at least one duty day."
            })

        unknown = describe_rule_errors(duty_days, day_configs)
        if unknown:
            raise serializers.ValidationError({
                "duty_days": (
                    "These duty days could not be understood: "
                    + ", ".join(unknown)
                    + ". Use weekdays (Mon, Tuesday), patterns like "
                    "'1st & 3rd Sundays', or a date (YYYY-MM-DD)."
                )
            })

        capacity = attrs.get("capacity", getattr(instance, "capacity", 15))
        attrs["total_weekly_capacity"] = compute_weekly_capacity(
            duty_days, day_configs, capacity
        )
        return attrs

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        doc_id_val = instance.doctor.doc_id if instance.doctor else ""

        ret["id"] = instance.sched_id
        ret["sched_id"] = instance.sched_id
        ret["doctorId"] = doc_id_val
        ret["doctor_id"] = doc_id_val
        ret["doctorName"] = instance.doctor_name
        ret["doctor_name"] = instance.doctor_name
        ret["specialty"] = instance.specialty
        ret["dutyDays"] = instance.duty_days or []
        ret["duty_days"] = instance.duty_days or []
        ret["dayConfigs"] = instance.day_configs or {}
        ret["day_configs"] = instance.day_configs or {}
        ret["shiftTime"] = instance.shift_time
        ret["shift_time"] = instance.shift_time
        ret["capacity"] = instance.capacity
        ret["dailyCapacity"] = instance.capacity
        weekly = instance.total_weekly_capacity or instance.capacity
        ret["totalWeeklyCapacity"] = weekly
        ret["total_weekly_capacity"] = weekly
        ret["status"] = instance.status
        return ret


# ============================================================
# BOOKING SERIALIZER
# ============================================================

class BookingSerializer(serializers.ModelSerializer):
    """
    ref_code is generated by the backend in create() and can never be
    changed afterwards.
    """

    ref_code = serializers.CharField(read_only=True, required=False)

    class Meta:
        model = Booking
        fields = "__all__"
        read_only_fields = ["ref_code", "created_at"]
        # Derived from the selected doctor in validate().
        extra_kwargs = {
            "doctor_name": {"required": False},
            "doctor_specialty": {"required": False},
        }

    # ---------------------------------------------------------------
    # helpers
    # ---------------------------------------------------------------

    def _generate_ref_code(self):
        while True:
            ref_code = f"ISALU-{uuid.uuid4().hex[:10].upper()}"
            if not Booking.objects.filter(ref_code=ref_code).exists():
                return ref_code

    @staticmethod
    def _error(message, **extra):
        payload = {"error": message}
        payload.update(extra)
        return serializers.ValidationError(payload)

    # ---------------------------------------------------------------
    # input
    # ---------------------------------------------------------------

    def to_internal_value(self, data):
        data_copy = _mutable_copy(data)
        _apply_aliases(data_copy, {
            "doctorId": "doctor_id",
            "doctorName": "doctor_name",
            "doctorSpecialty": "doctor_specialty",
            "patientName": "patient_name",
            "patientPhone": "patient_phone",
            "patientEmail": "patient_email",
            "paymentType": "payment_type",
            "hmoName": "hmo_name",
            "hmoPolicyCode": "hmo_policy_code",
            "hmoAuthCode": "hmo_auth_code",
            "referralDocName": "referral_doc_name",
            "referralDocData": "referral_doc_data",
            "referralDocText": "referral_doc_text",
            "hmoStatus": "hmo_status",
            "paymentStatus": "payment_status",
            "paymentMethod": "payment_method",
            "invoiceRef": "invoice_ref",
            "isActive": "is_active",
            "deleteReason": "delete_reason",
        })
        data_copy.pop("ref_code", None)
        data_copy.pop("refCode", None)

        # Doctor may be identified by name only (legacy clients).
        if not data_copy.get("doctor_id") and data_copy.get("doctor"):
            data_copy["doctor_id"] = data_copy.get("doctor")

        if data_copy.get("date"):
            parsed = parse_date(data_copy["date"])
            if parsed:
                data_copy["date"] = parsed.isoformat()

        return super().to_internal_value(data_copy)

    # ---------------------------------------------------------------
    # output
    # ---------------------------------------------------------------

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["refCode"] = instance.ref_code
        ret["ref_code"] = instance.ref_code
        ret["doctorId"] = instance.doctor_id
        ret["doctorName"] = instance.doctor_name
        ret["doctorSpecialty"] = instance.doctor_specialty
        ret["patientName"] = instance.patient_name
        ret["patientPhone"] = instance.patient_phone
        ret["patientEmail"] = instance.patient_email
        ret["paymentType"] = instance.payment_type
        ret["paymentStatus"] = instance.payment_status
        ret["paymentMethod"] = instance.payment_method
        ret["hmoName"] = instance.hmo_name
        ret["hmoPolicyCode"] = instance.hmo_policy_code
        ret["hmoAuthCode"] = instance.hmo_auth_code
        ret["hmoStatus"] = instance.hmo_status
        ret["referralDocName"] = instance.referral_doc_name
        ret["referralDocData"] = (
            instance.referral_doc_data
            if "referral_doc_data" in instance.__dict__
            else None
        )
        ret["referralDocText"] = (
            instance.referral_doc_text
            if "referral_doc_text" in instance.__dict__
            else None
        )
        ret["invoiceRef"] = instance.invoice_ref
        ret["isActive"] = instance.is_active
        ret["deleteReason"] = instance.delete_reason
        ret["createdAt"] = instance.created_at.isoformat() if instance.created_at else None
        return ret

    # ---------------------------------------------------------------
    # validation
    # ---------------------------------------------------------------

    def _identity_changed(self, data):
        inst = self.instance
        return any(
            key in data and str(data[key]).strip() != str(getattr(inst, key) or "").strip()
            for key in ("patient_name", "patient_phone")
        )

    def _reject_duplicate(self, doctor, name, phone):
        existing = find_duplicate_booking(
            doctor, name, phone, exclude_ref=self.instance.ref_code if self.instance else None
        )
        if existing is not None:
            raise self._error(
                duplicate_message(existing, existing.clinic_label),
                duplicate=True,
                existing_ref=existing.ref_code,
                existing_date=existing.date,
            )

    MAX_REFERRAL_BYTES = 5 * 1024 * 1024
    ALLOWED_REFERRAL_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/jpg", "image/webp")

    def validate_referral_doc_data(self, value):
        """Only PDF / image data URLs up to 5 MB are accepted."""
        if not value:
            return value
        value = str(value)
        match = re.match(r"^data:([\w/+.-]+);base64,", value)
        if not match or match.group(1).lower() not in self.ALLOWED_REFERRAL_TYPES:
            raise serializers.ValidationError("Referral letter must be a PDF, PNG or JPEG file.")
        approx_bytes = (len(value) - match.end()) * 3 // 4
        if approx_bytes > self.MAX_REFERRAL_BYTES:
            raise serializers.ValidationError("Referral letter is too large (maximum 5 MB).")
        return value

    def _scheduling_changed(self, data):
        if self.instance is None:
            return True
        inst = self.instance
        if "doctor_id" in data and str(data["doctor_id"]).strip().lower() != str(inst.doctor_id).strip().lower():
            return True
        if "date" in data and str(data["date"]) != str(inst.date):
            return True
        if "time" in data and str(data["time"]).strip() != str(inst.time).strip():
            return True
        # Re-activating a cancelled / disabled booking consumes capacity again.
        was_counted = inst.counts_toward_capacity
        new_active = data.get("is_active", inst.is_active)
        new_status = str(data.get("status", inst.status) or "").strip().lower()
        will_count = bool(new_active) and new_status not in INACTIVE_BOOKING_STATUSES
        return will_count and not was_counted

    def validate(self, data):
        """
        Scheduling rules (doctor active, on duty, capacity, no past dates,
        same-day 10-minute cutoff) run when a booking is created or when its
        doctor/date/time changes. Lifecycle edits such as status, payment or
        HMO updates never re-run them, so staff can update today's bookings.
        """
        data = super().validate(data)
        inst = self.instance

        if not self._scheduling_changed(data):
            # Editing the patient's name/phone must not create a duplicate either.
            if inst is not None and self._identity_changed(data) and inst.counts_toward_capacity:
                self._reject_duplicate(
                    _resolve_doctor(inst.doctor_id),
                    data.get("patient_name", inst.patient_name),
                    data.get("patient_phone", inst.patient_phone),
                )
            return data

        # ---- doctor --------------------------------------------------
        doc_ref = data.get("doctor_id") or (inst.doctor_id if inst else None)
        doc_obj = _resolve_doctor(doc_ref)
        if not doc_obj:
            doc_obj = _resolve_doctor(data.get("doctor_name") or (inst.doctor_name if inst else None))
        if not doc_obj:
            raise self._error("Selected doctor could not be found.")

        doctor_changed = inst is not None and doc_obj.doc_id != inst.doctor_id
        data["doctor_id"] = doc_obj.doc_id
        if doctor_changed or not data.get("doctor_name"):
            data["doctor_name"] = doc_obj.full_name or doc_obj.name
        if doctor_changed or not data.get("doctor_specialty"):
            data["doctor_specialty"] = (
                doc_obj.department.name if doc_obj.department else (doc_obj.specialty or "General Medicine")
            )

        display = doc_obj.full_name or doc_obj.name
        if not doc_obj.status:
            raise self._error(
                f"Doctor Profile Inactive: {display} is currently inactive "
                "or unavailable for appointments."
            )

        # ---- date ----------------------------------------------------
        raw_date = data.get("date") or (inst.date if inst else None)
        appt_date = parse_date(raw_date)
        if not appt_date:
            raise self._error("Invalid appointment date. Please use a valid calendar date.")
        data["date"] = appt_date.isoformat()

        now_local = timezone.localtime(timezone.now())
        if appt_date < now_local.date():
            raise self._error("Appointments cannot be booked for a past date.")

        # ---- duty day ------------------------------------------------
        resolved = resolve_doctor_day(doc_obj, appt_date)
        day_name = appt_date.strftime("%A")
        if resolved["reason"] == "no_active_schedule":
            raise self._error(
                f"Doctor schedule unavailable: {display} has no active "
                "consultation schedule. Please choose another specialist."
            )
        if resolved["reason"] in ("cancelled", "moved_out"):
            exc = resolved["exception"]
            if exc.action == "reschedule":
                raise self._error(
                    f"Clinic moved: the {display} clinic on {appt_date.strftime('%b %d, %Y')} "
                    f"has been moved to {parse_date(exc.new_date).strftime('%A, %b %d, %Y')}. "
                    "Please book the new date instead."
                )
            raise self._error(
                f"Clinic cancelled: the {display} clinic on {appt_date.strftime('%A, %b %d, %Y')} "
                "has been cancelled. Please choose another date."
            )
        if not resolved["on_duty"]:
            ordinals = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
            raise self._error(
                f"Doctor schedule unavailable: {display} is not on duty on "
                f"{day_name}, {appt_date.strftime('%b %d, %Y')} (the "
                f"{ordinals[week_of_month(appt_date)]} {day_name} of the month)."
            )

        # ---- one upcoming appointment per patient per clinic ---------
        self._reject_duplicate(
            doc_obj,
            data.get("patient_name", inst.patient_name if inst else ""),
            data.get("patient_phone", inst.patient_phone if inst else ""),
        )

        # ---- capacity ------------------------------------------------
        max_capacity = resolved["capacity"]
        existing = count_doctor_bookings(
            doc_obj.doc_id, appt_date, exclude_ref=inst.ref_code if inst else None
        )
        if existing >= max_capacity:
            raise self._error(
                f"Daily Shift Capacity Full: {display} has reached the maximum "
                f"daily patient capacity of {max_capacity} visits for "
                f"{appt_date.isoformat()}. Please select another date.",
                capacity=max_capacity,
                booked=existing,
                remaining=0,
                doctor_id=doc_obj.doc_id,
                date=appt_date.isoformat(),
            )

        # ---- same-day cutoff -----------------------------------------
        time_str = data.get("time") or (inst.time if inst else "")
        appt_time = parse_time_of_day(time_str)
        if appt_date == now_local.date() and appt_time:
            appointment_dt = now_local.replace(
                hour=appt_time.hour, minute=appt_time.minute, second=0, microsecond=0
            )
            if (appointment_dt - now_local).total_seconds() / 60.0 < 10:
                raise self._error(
                    "Same-Day Cutoff Restriction: Online bookings for today's "
                    "clinic must be placed at least 10 minutes prior to the "
                    "appointment time. Please select a future time or contact "
                    "hospital reception."
                )

        self._resolved_capacity = max_capacity
        return data

    # ---------------------------------------------------------------
    # create / update
    # ---------------------------------------------------------------

    def _locked_capacity_check(self, validated_data, exclude_ref=None):
        """
        Lock the doctor row and re-count, so two simultaneous requests can
        never push a doctor over capacity.
        """
        doc_id = validated_data.get("doctor_id")
        doctor = Doctor.objects.select_for_update().filter(doc_id=doc_id).first()
        if not doctor:
            raise self._error("Selected doctor could not be found.")

        date_val = validated_data.get("date")
        resolved = resolve_doctor_day(doctor, date_val)
        max_capacity = resolved["capacity"] if resolved["on_duty"] else 0
        existing = count_doctor_bookings(doctor.doc_id, date_val, exclude_ref=exclude_ref)
        if existing >= max_capacity:
            raise self._error(
                f"Daily Shift Capacity Full: {doctor.full_name or doctor.name} "
                f"already has {existing} bookings for {date_val}. Maximum "
                f"capacity is {max_capacity}.",
                capacity=max_capacity,
                booked=existing,
                remaining=0,
            )

    def create(self, validated_data):
        with transaction.atomic():
            if not validated_data.get("doctor_id"):
                raise self._error("A doctor is required.")
            self._locked_capacity_check(validated_data)
            self._reject_duplicate(
                Doctor.objects.filter(doc_id=validated_data.get("doctor_id")).select_related("department").first(),
                validated_data.get("patient_name"),
                validated_data.get("patient_phone"),
            )
            validated_data["ref_code"] = self._generate_ref_code()
            return Booking.objects.create(**validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("ref_code", None)
        validated_data.pop("refCode", None)

        with transaction.atomic():
            if getattr(self, "_resolved_capacity", None) is not None:
                merged = {
                    "doctor_id": validated_data.get("doctor_id", instance.doctor_id),
                    "date": validated_data.get("date", instance.date),
                }
                self._locked_capacity_check(merged, exclude_ref=instance.ref_code)
            return super().update(instance, validated_data)


class BookingListSerializer(BookingSerializer):
    """
    List view serializer. Excludes the base64 referral document blobs,
    which made the registry response huge. Documents remain available
    on detail retrieve.
    """

    class Meta(BookingSerializer.Meta):
        exclude = ("referral_doc_data", "referral_doc_text")
        fields = None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("referralDocData", None)
        data.pop("referralDocText", None)
        return data


# ============================================================
# HMO COMPANY SERIALIZER
# ============================================================

class HmoCompanySerializer(serializers.ModelSerializer):
    hmo_id = serializers.CharField(required=False)
    name = serializers.CharField(required=False)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = HmoCompany
        fields = "__all__"

    def to_internal_value(self, data):
        data_copy = _mutable_copy(data)

        if self.instance:
            data_copy["hmo_id"] = self.instance.hmo_id
        else:
            data_copy["hmo_id"] = (
                data_copy.get("hmo_id") or data_copy.get("id") or _generate_id("hmo")
            )
            if not data_copy.get("name"):
                raise serializers.ValidationError({"name": "HMO company name is required."})

        _apply_aliases(data_copy, {"contactPerson": "contact_person"})
        data_copy.pop("contactPerson", None)

        if not self.instance or "code" in data_copy:
            if not data_copy.get("code"):
                base = "".join(
                    ch for ch in str(data_copy.get("name") or "HMO").upper() if ch.isalnum()
                )[:8] or "HMO"
                data_copy["code"] = f"HMO-{base}"

        if not self.instance and not data_copy.get("contact_person"):
            data_copy["contact_person"] = "Pre-Auth Desk Officer"

        if "status" in data_copy:
            data_copy["status"] = parse_bool_status(data_copy["status"])

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["id"] = instance.hmo_id
        ret["contactPerson"] = instance.contact_person
        return ret


# ============================================================
# SYSTEM USER SERIALIZER
# ============================================================

MIN_PASSWORD_LENGTH = 6


def resolve_or_create_role(role_name):
    if not role_name:
        return None
    r_clean = str(role_name).strip()
    role_obj = (
        Role.objects.filter(name__iexact=r_clean).first()
        or Role.objects.filter(role_id__iexact=r_clean).first()
    )
    if role_obj:
        return role_obj

    lower = r_clean.lower()
    if "monitor" in lower or "controller" in lower:
        primary = "monitor"
    elif "hmo" in lower or "insurance" in lower:
        primary = "hmo"
    elif "cash" in lower or "billing" in lower:
        primary = "cashdesk"
    elif "analytics" in lower or "executive" in lower:
        primary = "analytics"
    else:
        primary = "helpdesk"

    return Role.objects.create(
        role_id=_generate_id("role"),
        name=r_clean,
        description=f"Custom role: {r_clean}",
        primary_desk=primary,
        allowed_desks=[primary],
        is_system_role=False,
        status=True,
    )


class SystemUserSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    user_id = serializers.SerializerMethodField()
    name = serializers.CharField(source="first_name", required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    role = serializers.CharField(required=False, allow_blank=True)
    desk = serializers.CharField(required=False, allow_blank=True)
    status = serializers.SerializerMethodField()
    last_active = serializers.SerializerMethodField()
    lastActive = serializers.SerializerMethodField()
    last_login = serializers.SerializerMethodField()
    lastLogin = serializers.SerializerMethodField()
    created_at = serializers.SerializerMethodField()
    createdAt = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "user_id", "name", "email", "password", "role", "desk",
            "status", "last_active", "lastActive", "last_login", "lastLogin",
            "created_at", "createdAt",
        ]

    def get_id(self, obj):
        return f"usr-{obj.id}"

    def get_user_id(self, obj):
        return f"usr-{obj.id}"

    def get_status(self, obj):
        return "Active" if obj.is_active else "Disabled"

    def get_last_login(self, obj):
        if obj.last_login:
            return obj.last_login.strftime("%Y-%m-%d %H:%M:%S")
        return "Never logged in"

    def get_lastLogin(self, obj):
        return self.get_last_login(obj)

    def get_last_active(self, obj):
        return self.get_last_login(obj)

    def get_lastActive(self, obj):
        return self.get_last_login(obj)

    def get_created_at(self, obj):
        return obj.date_joined.strftime("%Y-%m-%d %H:%M:%S") if obj.date_joined else ""

    def get_createdAt(self, obj):
        return self.get_created_at(obj)

    def validate_email(self, value):
        value = (value or "").strip().lower()
        if not value:
            return value
        qs = User.objects.filter(Q_email_or_username(value))
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Another staff account already uses this email.")
        return value

    def validate_password(self, value):
        if value and len(value) < MIN_PASSWORD_LENGTH:
            raise serializers.ValidationError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        return value

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret.pop("password", None)

        role_name = "Helpdesk Officer"
        desk_name = "helpdesk"
        profile = getattr(instance, "profile", None) if hasattr(instance, "profile") else None
        if profile and profile.role:
            role_name = profile.role.name
            desk_name = profile.role.primary_desk
        elif instance.is_superuser:
            role_name = "Super Administrator"
            desk_name = "analytics"

        ret["role"] = role_name
        ret["desk"] = desk_name
        ret["name"] = instance.first_name or instance.username
        return ret

    def create(self, validated_data):
        initial = self.initial_data or {}
        raw_password = validated_data.get("password") or initial.get("password") or ""
        if not raw_password:
            raise serializers.ValidationError({"password": "A password is required for new staff accounts."})

        email = (validated_data.get("email") or "").strip().lower()
        name = validated_data.get("first_name") or initial.get("name") or (
            email.split("@")[0] if email else "Staff User"
        )
        is_active = parse_bool_status(initial.get("status"), default=True)
        username = email or f"user_{int(time.time() * 1000)}"

        user = User.objects.create_user(
            username=username,
            email=email,
            password=raw_password,
            first_name=name,
            is_staff=True,
            is_active=is_active,
        )
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = resolve_or_create_role(initial.get("role") or "Helpdesk Officer")
        profile.save()
        return user

    def update(self, instance, validated_data):
        initial = self.initial_data or {}

        email = (validated_data.get("email") or "").strip().lower()
        if email:
            instance.email = email
            instance.username = email

        name = validated_data.get("first_name") or initial.get("name")
        if name:
            instance.first_name = name

        password = validated_data.get("password")
        if password:
            instance.set_password(password)

        if "status" in initial and initial.get("status") not in (None, ""):
            instance.is_active = parse_bool_status(initial.get("status"), default=instance.is_active)

        instance.save()

        role_name = initial.get("role")
        if role_name:
            profile, _ = UserProfile.objects.get_or_create(user=instance)
            profile.role = resolve_or_create_role(role_name)
            profile.save()
        return instance


def Q_email_or_username(value):
    from django.db.models import Q
    return Q(email__iexact=value) | Q(username__iexact=value)


# ============================================================
# CUSTOM TIME SLOT SERIALIZER
# ============================================================

class CustomTimeSlotSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="slot_id", read_only=True)
    slot_id = serializers.CharField(required=False)

    class Meta:
        model = CustomTimeSlot
        fields = ["id", "slot_id", "label", "created_at"]
        read_only_fields = ["created_at"]

    def to_internal_value(self, data):
        data_copy = _mutable_copy(data)
        if not data_copy.get("slot_id"):
            data_copy["slot_id"] = data_copy.get("id") or _generate_id("slot")
        if not data_copy.get("label"):
            start = str(data_copy.get("startTime") or "").strip()
            end = str(data_copy.get("endTime") or "").strip()
            label = str(data_copy.get("formatted") or "").strip()
            if not label and start and end:
                suffix = str(data_copy.get("shiftLabel") or "").strip()
                label = f"{start} – {end}" + (f" ({suffix})" if suffix else "")
            data_copy["label"] = label
        return super().to_internal_value(data_copy)

    def create(self, validated_data):
        existing = CustomTimeSlot.objects.filter(label=validated_data.get("label")).first()
        if existing:
            return existing
        return super().create(validated_data)


# ============================================================
# ROLE SERIALIZER
# ============================================================

class RoleSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="role_id", read_only=True)
    role_id = serializers.CharField(required=False)
    primary_desk = serializers.CharField(required=False)
    allowed_desks = serializers.JSONField(required=False)
    is_system_role = serializers.BooleanField(required=False)
    status = serializers.BooleanField(required=False)

    class Meta:
        model = Role
        fields = [
            "id", "role_id", "name", "description", "primary_desk",
            "allowed_desks", "is_system_role", "status", "created_at",
        ]
        read_only_fields = ["created_at"]

    def to_internal_value(self, data):
        data = _mutable_copy(data)
        _apply_aliases(data, {
            "primaryDesk": "primary_desk",
            "allowedDesks": "allowed_desks",
            "isSystemRole": "is_system_role",
        })

        if self.instance:
            data["role_id"] = self.instance.role_id
        elif not data.get("role_id"):
            data["role_id"] = data.get("id") or _generate_id("role")

        # Only default status on create; a PATCH without status must not
        # silently re-activate a disabled role.
        if "status" in data:
            data["status"] = parse_bool_status(data["status"])
        elif not self.instance:
            data["status"] = True

        if not self.instance:
            data.setdefault("primary_desk", "helpdesk")
            data.setdefault("allowed_desks", [data.get("primary_desk") or "helpdesk"])

        return super().to_internal_value(data)

    def validate_name(self, value):
        value = str(value).strip()
        qs = Role.objects.filter(name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A role with this name already exists.")
        return value

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["id"] = instance.role_id
        ret["role_id"] = instance.role_id
        ret["primaryDesk"] = instance.primary_desk
        ret["primary_desk"] = instance.primary_desk
        ret["allowedDesks"] = instance.allowed_desks or []
        ret["allowed_desks"] = instance.allowed_desks or []
        ret["isSystemRole"] = instance.is_system_role
        ret["is_system_role"] = instance.is_system_role
        ret["createdAt"] = ret.get("created_at")
        ret["status"] = "Active" if instance.status else "Disabled"
        return ret


# ============================================================
# APP SETTING SERIALIZER
# ============================================================

class AppSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppSetting
        fields = "__all__"


# ============================================================
# SCHEDULE EXCEPTION SERIALIZER (read-only; writes go through
# api.schedule_changes so bookings and notifications stay in sync)
# ============================================================

class ScheduleExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScheduleException
        fields = "__all__"
        read_only_fields = [f.name for f in ScheduleException._meta.fields]

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        doctor = instance.doctor
        ret["id"] = instance.exception_id
        ret["doctorId"] = doctor.doc_id
        ret["doctorName"] = doctor.full_name or doctor.name
        ret["doctorAcronym"] = doctor.acronym or doctor.name
        ret["specialty"] = doctor.department.name if doctor.department else doctor.specialty
        ret["originalDate"] = instance.original_date
        ret["newDate"] = instance.new_date
        ret["shiftTimes"] = instance.shift_times or []
        ret["affectedCount"] = instance.affected_count
        ret["notificationStatus"] = instance.notification_status
        ret["notifiedCount"] = instance.notified_count
        ret["notificationFailedCount"] = instance.notification_failed_count
        ret["createdAt"] = instance.created_at.isoformat() if instance.created_at else None
        return ret
