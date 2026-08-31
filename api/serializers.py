from api.models import AppSetting

from rest_framework import serializers
from django.contrib.auth.models import User

import time

from .models import (
    Department,
    Doctor,
    SpecialistSchedule,
    Booking,
    HmoCompany,
    CustomTimeSlot,
    Role,
    UserProfile,
)


# ============================================================
# COMMON HELPERS
# ============================================================

def parse_bool_status(val, default=True):
    """
    Convert frontend status values such as:
        Active
        Disabled
        true
        false
        1
        0
        On Duty
        Off Duty
    into Python booleans.
    """

    if val is None:
        return default

    if isinstance(val, bool):
        return val

    s = str(val).strip().lower()

    if s in (
        "true",
        "1",
        "active",
        "active partner",
        "active on duty",
        "confirmed",
        "yes",
        "enabled",
        "on duty",
    ):
        return True

    if s in (
        "false",
        "0",
        "inactive",
        "disabled",
        "off duty",
        "cancelled",
        "no",
        "maintenance",
        "under maintenance",
    ):
        return False

    return default


# ============================================================
# DEPARTMENT SERIALIZER
# ============================================================

class DepartmentSerializer(serializers.ModelSerializer):
    id = serializers.CharField(
        source="dept_id",
        read_only=True
    )

    class Meta:
        model = Department
        fields = "__all__"

    def to_internal_value(self, data):
        data_copy = (
            data.copy()
            if hasattr(data, "copy")
            else dict(data)
        )

        if "status" in data_copy:
            data_copy["status"] = parse_bool_status(
                data_copy["status"]
            )

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)

        ret["id"] = instance.dept_id
        ret["dept_id"] = instance.dept_id

        ret["status"] = instance.status

        ret["location"] = (
            getattr(instance, "location", None)
            or "Main Building"
        )

        doc_count = instance.doctors.count()

        ret["doctor_count"] = (
            doc_count
            if doc_count > 0
            else (instance.doctor_count or 0)
        )

        ret["doctorCount"] = ret["doctor_count"]

        return ret


# ============================================================
# DOCTOR SERIALIZER
# ============================================================

class DoctorSerializer(serializers.ModelSerializer):
    doc_id = serializers.CharField(
        required=False
    )

    active_booking_count = serializers.SerializerMethodField()

    name = serializers.CharField(
        required=False,
        allow_blank=True
    )

    class Meta:
        model = Doctor
        fields = "__all__"

    # --------------------------------------------------------
    # ACTIVE BOOKING COUNT
    # --------------------------------------------------------

    def get_active_booking_count(self, obj):
        """
        Return the number of active bookings for this doctor.

        IMPORTANT:
        Booking.doctor_id stores the Doctor.doc_id value,
        not Django's numeric primary key.
        """

        try:
            return Booking.objects.filter(
                doctor_id=obj.doc_id,
                is_active=True
            ).exclude(
                status__iexact="Disabled"
            ).count()

        except Exception:
            # Prevent the entire doctor endpoint from crashing
            # if an unexpected database/model condition occurs.
            return 0

    # --------------------------------------------------------
    # INPUT NORMALIZATION
    # --------------------------------------------------------

    def to_internal_value(self, data):
        data_copy = (
            data.copy()
            if hasattr(data, "copy")
            else dict(data)
        )

        # ----------------------------------------------------
        # Generate / preserve doctor ID
        # ----------------------------------------------------

        if self.instance:
            doc_id_val = (
                getattr(self.instance, "doc_id", None)
                or data_copy.get("doc_id")
                or data_copy.get("id")
            )
        else:
            doc_id_val = (
                data_copy.get("doc_id")
                or data_copy.get("id")
                or (
                    f"doc-{int(time.time() * 1000)}-"
                    f"{__import__('random').randint(100, 999)}"
                )
            )

        if doc_id_val:
            data_copy["doc_id"] = doc_id_val

        # ----------------------------------------------------
        # Doctor name
        # ----------------------------------------------------

        if not data_copy.get("name") and not self.instance:
            data_copy["name"] = (
                data_copy.get("fullName")
                or data_copy.get("full_name")
                or "Doctor"
            )

        # ----------------------------------------------------
        # CamelCase → snake_case
        # ----------------------------------------------------

        mapping = {
            "fullName": "full_name",
            "availableDays": "available_days",
            "availability": "available_days",
            "timeSlots": "time_slots",
            "roomNumber": "room_number",
            "room": "room_number",
            "acceptedPatientTypes": "accepted_patient_types",
            "accepted_patient_types": "accepted_patient_types",
        }

        for camel, snake in mapping.items():
            if camel in data_copy:
                if (
                    snake not in data_copy
                    or not data_copy[snake]
                ):
                    data_copy[snake] = data_copy[camel]

        # ----------------------------------------------------
        # Department
        # ----------------------------------------------------

        dept_val = (
            data_copy.get("department_id")
            or data_copy.get("departmentId")
            or data_copy.get("department")
        )

        if dept_val:

            if isinstance(dept_val, dict):
                dept_str = str(
                    dept_val.get("dept_id")
                    or dept_val.get("id")
                    or dept_val.get("name")
                    or ""
                ).strip()
            else:
                dept_str = str(dept_val).strip()

            dept_obj = None

            if dept_str:
                dept_obj = (
                    Department.objects
                    .filter(
                        dept_id__iexact=dept_str
                    )
                    .first()
                )

            if not dept_obj and dept_str:
                dept_obj = (
                    Department.objects
                    .filter(
                        name__icontains=dept_str
                    )
                    .first()
                )

            if dept_obj:
                data_copy["department"] = (
                    dept_obj.dept_id
                )

            else:
                if (
                    self.instance
                    and self.instance.department
                ):
                    data_copy["department"] = (
                        self.instance.department.dept_id
                    )
                else:
                    data_copy["department"] = None

        elif (
            self.instance
            and self.instance.department
        ):
            data_copy["department"] = (
                self.instance.department.dept_id
            )

        # ----------------------------------------------------
        # Status
        # ----------------------------------------------------

        if "status" in data_copy:
            data_copy["status"] = parse_bool_status(
                data_copy["status"]
            )

        return super().to_internal_value(data_copy)

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    def to_representation(self, instance):
        ret = super().to_representation(instance)

        ret["id"] = instance.doc_id
        ret["doc_id"] = instance.doc_id

        ret["fullName"] = (
            instance.full_name
            or instance.name
        )

        ret["full_name"] = (
            instance.full_name
            or instance.name
        )

        ret["departmentId"] = (
            instance.department.dept_id
            if instance.department
            else (
                getattr(
                    instance,
                    "department_id",
                    None
                )
                or ""
            )
        )

        ret["department_id"] = ret["departmentId"]

        # ----------------------------------------------------
        # Department object
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Availability
        # ----------------------------------------------------

        ret["availableDays"] = (
            instance.available_days
            or []
        )

        ret["availability"] = (
            instance.available_days
            or []
        )

        ret["timeSlots"] = (
            instance.time_slots
            or []
        )

        # ----------------------------------------------------
        # Room
        # ----------------------------------------------------

        ret["roomNumber"] = (
            instance.room_number
            or ""
        )

        ret["room"] = (
            instance.room_number
            or ""
        )

        # ----------------------------------------------------
        # Patient types
        # ----------------------------------------------------

        types = instance.accepted_patient_types

        if not types or len(types) == 0:
            types = [
                "Private Self-Pay",
                "HMO Insurance",
            ]

        ret["acceptedPatientTypes"] = types
        ret["accepted_patient_types"] = types

        # ----------------------------------------------------
        # Status
        # ----------------------------------------------------

        ret["status"] = instance.status

        # ----------------------------------------------------
        # ACTIVE BOOKINGS
        #
        # Explicitly add it to the response.
        # This guarantees the frontend receives it.
        # ----------------------------------------------------

        ret["active_booking_count"] = (
            self.get_active_booking_count(instance)
        )

        ret["activeBookingCount"] = (
            ret["active_booking_count"]
        )

        return ret


# ============================================================
# SPECIALIST / DOCTOR SCHEDULE SERIALIZER
# ============================================================

class SpecialistScheduleSerializer(
    serializers.ModelSerializer
):
    sched_id = serializers.CharField(
        required=False
    )

    class Meta:
        model = SpecialistSchedule
        fields = "__all__"

    # --------------------------------------------------------
    # INPUT
    # --------------------------------------------------------

    def to_internal_value(self, data):
        data_copy = (
            data.copy()
            if hasattr(data, "copy")
            else dict(data)
        )

        # ----------------------------------------------------
        # Schedule ID
        # ----------------------------------------------------

        if self.instance:
            sched_id_val = (
                getattr(
                    self.instance,
                    "sched_id",
                    None
                )
                or data_copy.get("sched_id")
                or data_copy.get("id")
            )

        else:
            sched_id_val = (
                data_copy.get("sched_id")
                or data_copy.get("id")
                or (
                    f"sched-{int(time.time() * 1000)}-"
                    f"{__import__('random').randint(100, 999)}"
                )
            )

        if sched_id_val:
            data_copy["sched_id"] = sched_id_val

        # ----------------------------------------------------
        # CamelCase → snake_case
        # ----------------------------------------------------

        mapping = {
            "doctorName": "doctor_name",
            "dutyDays": "duty_days",
            "dayConfigs": "day_configs",
            "shiftTime": "shift_time",
            "totalWeeklyCapacity": "total_weekly_capacity",
        }

        for camel, snake in mapping.items():
            if camel in data_copy:
                if (
                    snake not in data_copy
                    or not data_copy[snake]
                ):
                    data_copy[snake] = data_copy[camel]

        # ----------------------------------------------------
        # Doctor
        # ----------------------------------------------------

        doc_val = (
            data_copy.get("doctor_id")
            or data_copy.get("doctorId")
            or data_copy.get("doctor")
        )

        if doc_val:

            if isinstance(doc_val, dict):

                doc_str = str(
                    doc_val.get("doc_id")
                    or doc_val.get("id")
                    or doc_val.get("name")
                    or ""
                ).strip()

            else:
                doc_str = str(doc_val).strip()

            doc_obj = None

            if doc_str:
                doc_obj = (
                    Doctor.objects
                    .filter(
                        doc_id__iexact=doc_str
                    )
                    .first()
                )

            if not doc_obj and doc_str:
                doc_obj = (
                    Doctor.objects
                    .filter(
                        name__iexact=doc_str
                    )
                    .first()
                    or Doctor.objects.filter(
                        full_name__iexact=doc_str
                    ).first()
                )

            if doc_obj:
                data_copy["doctor"] = (
                    doc_obj.doc_id
                )

            else:
                if (
                    self.instance
                    and self.instance.doctor
                ):
                    data_copy["doctor"] = (
                        self.instance.doctor.doc_id
                    )
                else:
                    data_copy["doctor"] = None

        elif (
            self.instance
            and self.instance.doctor
        ):
            data_copy["doctor"] = (
                self.instance.doctor.doc_id
            )

        # ----------------------------------------------------
        # Status
        # ----------------------------------------------------

        if "status" in data_copy:
            data_copy["status"] = parse_bool_status(
                data_copy["status"]
            )

        return super().to_internal_value(data_copy)

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    def to_representation(self, instance):
        ret = super().to_representation(instance)

        doc_id_val = (
            instance.doctor.doc_id
            if instance.doctor
            else ""
        )

        doc_name_val = instance.doctor_name
        specialty_val = instance.specialty

        ret["id"] = instance.sched_id
        ret["sched_id"] = instance.sched_id

        ret["doctorId"] = doc_id_val
        ret["doctor_id"] = doc_id_val

        ret["doctorName"] = doc_name_val
        ret["doctor_name"] = doc_name_val

        ret["specialty"] = specialty_val

        ret["dutyDays"] = (
            instance.duty_days
            or []
        )

        ret["duty_days"] = (
            instance.duty_days
            or []
        )

        ret["dayConfigs"] = (
            instance.day_configs
            or {}
        )

        ret["day_configs"] = (
            instance.day_configs
            or {}
        )

        ret["shiftTime"] = instance.shift_time
        ret["shift_time"] = instance.shift_time

        ret["totalWeeklyCapacity"] = (
            instance.total_weekly_capacity
            or instance.capacity
        )

        ret["total_weekly_capacity"] = (
            instance.total_weekly_capacity
            or instance.capacity
        )

        ret["status"] = instance.status

        return ret


# ============================================================
# BOOKING SERIALIZER
# ============================================================

class BookingSerializer(serializers.ModelSerializer):

    class Meta:
        model = Booking
        fields = "__all__"

    # --------------------------------------------------------
    # INPUT
    # --------------------------------------------------------

    def to_internal_value(self, data):

        data_copy = (
            data.copy()
            if hasattr(data, "copy")
            else dict(data)
        )

        mapping = {
            "refCode": "ref_code",
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
        }

        for camel, snake in mapping.items():

            if camel in data_copy:

                if (
                    snake not in data_copy
                    or data_copy[snake] is None
                    or data_copy[snake] == ""
                ):
                    data_copy[snake] = data_copy[camel]

        return super().to_internal_value(data_copy)

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    def to_representation(self, instance):

        ret = super().to_representation(instance)

        ret["refCode"] = instance.ref_code
        ret["doctorId"] = instance.doctor_id
        ret["doctorName"] = instance.doctor_name
        ret["doctorSpecialty"] = instance.doctor_specialty

        ret["patientName"] = instance.patient_name
        ret["patientPhone"] = instance.patient_phone
        ret["patientEmail"] = instance.patient_email

        ret["paymentType"] = instance.payment_type

        ret["hmoName"] = instance.hmo_name
        ret["hmoPolicyCode"] = instance.hmo_policy_code
        ret["hmoAuthCode"] = instance.hmo_auth_code

        ret["referralDocName"] = instance.referral_doc_name
        ret["referralDocData"] = instance.referral_doc_data
        ret["referralDocText"] = instance.referral_doc_text

        ret["hmoStatus"] = instance.hmo_status

        ret["paymentStatus"] = instance.payment_status
        ret["paymentMethod"] = instance.payment_method

        ret["invoiceRef"] = instance.invoice_ref

        ret["isActive"] = instance.is_active
        ret["deleteReason"] = instance.delete_reason

        ret["createdAt"] = (
            instance.created_at.isoformat()
            if instance.created_at
            else None
        )

        return ret

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    def validate(self, data):

        data = super().validate(data)

        date_str = data.get("date")
        time_str = data.get("time")

        doc_id = (
            data.get("doctor_id")
            or data.get("doctorId")
            or data.get("doctor")
        )

        doc_name = (
            data.get("doctor_name")
            or data.get("doctorName")
        )

        # ====================================================
        # FIND DOCTOR
        # ====================================================

        doc_obj = None

        if doc_id:

            if isinstance(doc_id, Doctor):
                doc_obj = doc_id

            else:
                doc_obj = (
                    Doctor.objects
                    .filter(
                        doc_id__iexact=str(
                            doc_id
                        ).strip()
                    )
                    .first()
                )

        if not doc_obj and doc_name:

            clean_name = str(
                doc_name
            ).strip()

            doc_obj = (
                Doctor.objects
                .filter(
                    name__iexact=clean_name
                )
                .first()
                or Doctor.objects.filter(
                    full_name__iexact=clean_name
                ).first()
            )

        # ====================================================
        # DOCTOR STATUS / SCHEDULE VALIDATION
        # ====================================================

        if doc_obj:

            if not doc_obj.status:

                raise serializers.ValidationError({
                    "error": (
                        "Doctor Profile Inactive: "
                        f"{doc_obj.full_name or doc_obj.name} "
                        "is currently inactive or unavailable "
                        "for appointments."
                    )
                })

            sched_obj = (
                doc_obj.schedules.first()
            )

            if sched_obj and not sched_obj.status:

                raise serializers.ValidationError({
                    "error": (
                        "Schedule Suspended: Clinic schedule "
                        f"for {doc_obj.full_name or doc_obj.name} "
                        "is currently suspended or on leave."
                    )
                })

            # =================================================
            # DAILY CAPACITY
            # =================================================

            if date_str:

                import datetime

                dt_obj = None

                try:
                    dt_obj = datetime.datetime.strptime(
                        str(date_str),
                        "%Y-%m-%d"
                    )

                except Exception:
                    pass

                day_short = (
                    dt_obj.strftime("%a")
                    if dt_obj
                    else ""
                )

                max_capacity = 15

                if sched_obj:

                    day_cfgs = (
                        sched_obj.day_configs
                        or {}
                    )

                    if (
                        day_short in day_cfgs
                        and isinstance(
                            day_cfgs[day_short],
                            dict
                        )
                    ):

                        max_capacity = int(
                            day_cfgs[
                                day_short
                            ].get(
                                "capacity"
                            )
                            or sched_obj.capacity
                            or 15
                        )

                    elif sched_obj.capacity:

                        max_capacity = int(
                            sched_obj.capacity
                        )

                # ---------------------------------------------
                # DUTY DAYS
                # ---------------------------------------------

                if sched_obj:

                    duty_days = (
                        sched_obj.duty_days
                        or []
                    )

                    day_name = (
                        dt_obj.strftime("%A")
                        if dt_obj
                        else ""
                    )

                    day_short = (
                        dt_obj.strftime("%a")
                        if dt_obj
                        else ""
                    )

                    tokens = [
                        str(x).strip().lower()
                        for x in duty_days
                    ]

                    if tokens:

                        is_on_duty = any(
                            t == day_name.lower()
                            or t == day_short.lower()
                            or day_name.lower().startswith(t)
                            for t in tokens
                        )

                        if not is_on_duty:

                            raise serializers.ValidationError({
                                "error": (
                                    "Doctor schedule unavailable: "
                                    f"{doc_obj.full_name or doc_obj.name} "
                                    f"is not on duty on {day_name}."
                                )
                            })

                # ---------------------------------------------
                # EXISTING BOOKINGS
                # ---------------------------------------------

                existing_count = (
                    Booking.objects
                    .filter(
                        doctor_id=doc_obj.doc_id,
                        date=date_str,
                        is_active=True
                    )
                    .exclude(
                        status="Disabled"
                    )
                    .count()
                )

                if existing_count >= max_capacity:

                    raise serializers.ValidationError({
                        "error": (
                            "Daily Shift Capacity Full: "
                            f"{doc_obj.full_name or doc_obj.name} "
                            "has reached maximum daily patient "
                            f"capacity ({max_capacity} visits) "
                            f"for {date_str}. Please select "
                            "another date."
                        )
                    })

        # ====================================================
        # DATE VALIDATION
        # ====================================================

        if date_str:

            import datetime

            parsed_date = None

            raw_date = str(
                date_str
            ).strip()

            for fmt in (
                "%Y-%m-%d",
                "%A, %B %d, %Y",
                "%a, %b %d, %Y",
                "%A, %b %d, %Y",
            ):

                try:

                    parsed_date = (
                        datetime.datetime
                        .strptime(
                            raw_date,
                            fmt
                        )
                        .date()
                    )

                    break

                except ValueError:
                    continue

            if not parsed_date:

                raise serializers.ValidationError({
                    "error": (
                        "Invalid appointment date. "
                        "Please use a valid calendar date."
                    )
                })

            data["date"] = (
                parsed_date.isoformat()
            )

            date_str = data["date"]

        # ====================================================
        # DUPLICATE APPOINTMENT
        # ====================================================

        if doc_obj and date_str and time_str:

            duplicate = (
                Booking.objects
                .filter(
                    doctor_id=doc_obj.doc_id,
                    date=date_str,
                    time=time_str,
                    is_active=True
                )
                .exclude(
                    status="Disabled"
                )
            )

            if self.instance:

                duplicate = duplicate.exclude(
                    ref_code=self.instance.ref_code
                )

            if duplicate.exists():

                raise serializers.ValidationError({
                    "error": (
                        "The selected doctor already has "
                        "an appointment at this date and "
                        "time. Please choose another time."
                    )
                })

        # ====================================================
        # SAME-DAY 30-MINUTE CUTOFF
        # ====================================================

        if date_str and time_str:

            import datetime
            import re

            from django.utils import timezone

            now_local = timezone.localtime(
                timezone.now()
            )

            today_str = (
                now_local.strftime("%Y-%m-%d")
            )

            if date_str == today_str:

                match = re.search(
                    r"(\d{1,2}):(\d{2})\s*(AM|PM)?",
                    str(time_str),
                    re.IGNORECASE
                )

                if match:

                    hour = int(
                        match.group(1)
                    )

                    minute = int(
                        match.group(2)
                    )

                    ampm = match.group(3)

                    if ampm:

                        ampm = ampm.upper()

                        if (
                            ampm == "PM"
                            and hour < 12
                        ):
                            hour += 12

                        elif (
                            ampm == "AM"
                            and hour == 12
                        ):
                            hour = 0

                    clinic_start = (
                        now_local.replace(
                            hour=hour,
                            minute=minute,
                            second=0,
                            microsecond=0
                        )
                    )

                    time_diff_minutes = (
                        (
                            clinic_start
                            - now_local
                        ).total_seconds()
                        / 60.0
                    )

                    if time_diff_minutes < 30:

                        raise serializers.ValidationError({
                            "error": (
                                "Same-Day Cutoff Restriction: "
                                "Online bookings for today's "
                                "clinic must be placed at least "
                                "30 minutes prior to the clinic "
                                "start time. Please select a "
                                "future date or contact hospital "
                                "reception."
                            )
                        })

        return data

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    def create(self, validated_data):

        if not validated_data.get(
            "ref_code"
        ):

            import uuid

            validated_data["ref_code"] = (
                f"ISALU-"
                f"{uuid.uuid4().hex[:10].upper()}"
            )

        return super().create(
            validated_data
        )


# ============================================================
# HMO COMPANY SERIALIZER
# ============================================================

class HmoCompanySerializer(
    serializers.ModelSerializer
):

    hmo_id = serializers.CharField(
        required=False
    )

    name = serializers.CharField(
        required=False
    )

    class Meta:
        model = HmoCompany
        fields = "__all__"

    def to_internal_value(self, data):

        data_copy = (
            data.copy()
            if hasattr(data, "copy")
            else dict(data)
        )

        # ----------------------------------------------------
        # HMO ID
        # ----------------------------------------------------

        if self.instance:

            hmo_id_val = (
                getattr(
                    self.instance,
                    "hmo_id",
                    None
                )
                or data_copy.get("hmo_id")
                or data_copy.get("id")
            )

        else:

            hmo_id_val = (
                data_copy.get("hmo_id")
                or data_copy.get("id")
                or f"hmo-{int(time.time() * 1000)}"
            )

        if hmo_id_val:
            data_copy["hmo_id"] = hmo_id_val

        # ----------------------------------------------------
        # CODE
        # ----------------------------------------------------

        if not data_copy.get("code"):

            base = "".join(
                ch
                for ch in str(
                    data_copy.get("name")
                    or "HMO"
                ).upper()
                if ch.isalnum()
            )[:8] or "HMO"

            data_copy["code"] = (
                f"HMO-{base}"
            )

        # ----------------------------------------------------
        # CONTACT PERSON
        # ----------------------------------------------------

        contact = (
            data_copy.get("contact_person")
            or data_copy.get("contactPerson")
            or "Pre-Auth Desk Officer"
        )

        data_copy["contact_person"] = contact

        # ----------------------------------------------------
        # CamelCase
        # ----------------------------------------------------

        mapping = {
            "contactPerson": "contact_person",
        }

        for camel, snake in mapping.items():

            if camel in data_copy:

                if (
                    snake not in data_copy
                    or not data_copy[snake]
                ):
                    data_copy[snake] = (
                        data_copy[camel]
                    )

                data_copy.pop(
                    camel,
                    None
                )

        return super().to_internal_value(
            data_copy
        )

    def to_representation(self, instance):

        ret = super().to_representation(
            instance
        )

        ret["id"] = instance.hmo_id

        ret["contactPerson"] = (
            instance.contact_person
        )

        return ret


# ============================================================
# SYSTEM USER SERIALIZER
# ============================================================

class SystemUserSerializer(
    serializers.ModelSerializer
):

    id = serializers.SerializerMethodField()

    user_id = serializers.SerializerMethodField()

    name = serializers.CharField(
        source="first_name",
        required=False,
        allow_blank=True
    )

    email = serializers.EmailField(
        required=False,
        allow_blank=True
    )

    password = serializers.CharField(
        write_only=True,
        required=False
    )

    role = serializers.CharField(
        required=False,
        allow_blank=True
    )

    desk = serializers.CharField(
        required=False,
        allow_blank=True
    )

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
            "id",
            "user_id",
            "name",
            "email",
            "password",
            "role",
            "desk",
            "status",
            "last_active",
            "lastActive",
            "last_login",
            "lastLogin",
            "created_at",
            "createdAt",
        ]

    # --------------------------------------------------------
    # METHODS
    # --------------------------------------------------------

    def get_id(self, obj):
        return f"usr-{obj.id}"

    def get_user_id(self, obj):
        return f"usr-{obj.id}"

    def get_status(self, obj):
        return (
            "Active"
            if obj.is_active
            else "Disabled"
        )

    def get_last_login(self, obj):

        if obj.last_login:

            return obj.last_login.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        if obj.date_joined:

            return obj.date_joined.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        return "Never logged in"

    def get_lastLogin(self, obj):
        return self.get_last_login(obj)

    def get_last_active(self, obj):
        return self.get_last_login(obj)

    def get_lastActive(self, obj):
        return self.get_last_login(obj)

    def get_created_at(self, obj):

        if obj.date_joined:

            return obj.date_joined.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        return ""

    def get_createdAt(self, obj):
        return self.get_created_at(obj)

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    def to_representation(self, instance):

        ret = super().to_representation(
            instance
        )

        ret.pop("password", None)
        ret.pop("password_hash", None)
        ret.pop("user_password", None)

        role_name = "Helpdesk Officer"
        desk_name = "helpdesk"

        if (
            hasattr(instance, "profile")
            and instance.profile
            and instance.profile.role
        ):

            role_name = (
                instance.profile.role.name
            )

            desk_name = (
                instance.profile.role.primary_desk
            )

        elif instance.is_superuser:

            role_name = (
                "Super Administrator"
            )

            desk_name = "analytics"

        ret["role"] = role_name
        ret["desk"] = desk_name

        ret["name"] = (
            instance.first_name
            or instance.username
        )

        return ret

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    def create(self, validated_data):

        initial_data = (
            self.initial_data
            or {}
        )

        raw_password = (
            validated_data.get("password")
            or initial_data.get("password")
            or "admin123"
        )

        email = (
            validated_data.get("email")
            or initial_data.get("email")
            or ""
        ).strip().lower()

        name = (
            validated_data.get("first_name")
            or initial_data.get("name")
            or (
                email.split("@")[0]
                if email
                else "Staff User"
            )
        )

        role_name = (
            initial_data.get("role")
            or "Helpdesk Officer"
        )

        status_input = (
            initial_data.get(
                "status",
                "Active"
            )
        )

        clean_username = (
            email
            if email
            else f"user_{int(time.time() * 1000)}".lower()
        )

        # ----------------------------------------------------
        # FIND EXISTING USER
        # ----------------------------------------------------

        user = None

        if email:

            user = (
                User.objects
                .filter(
                    email__iexact=email
                )
                .first()
            )

        if not user:

            user = (
                User.objects
                .filter(
                    username__iexact=clean_username
                )
                .first()
            )

        # ----------------------------------------------------
        # UPDATE EXISTING USER
        # ----------------------------------------------------

        if user:

            user.first_name = name

            if raw_password:
                user.set_password(
                    raw_password
                )

            user.is_staff = True

            user.is_active = (
                status_input
                != "Disabled"
            )

            user.save()

        # ----------------------------------------------------
        # CREATE USER
        # ----------------------------------------------------

        else:

            user = User.objects.create_user(
                username=clean_username,
                email=email,
                password=raw_password,
                first_name=name,
                is_staff=True,
                is_active=(
                    status_input
                    != "Disabled"
                )
            )

        # ----------------------------------------------------
        # ROLE
        # ----------------------------------------------------

        role_obj = (
            self._resolve_or_create_role(
                role_name
            )
        )

        profile, _ = (
            UserProfile.objects
            .get_or_create(
                user=user
            )
        )

        if role_obj:

            profile.role = role_obj
            profile.save()

        return user

    # --------------------------------------------------------
    # RESOLVE ROLE
    # --------------------------------------------------------

    def _resolve_or_create_role(
        self,
        role_name
    ):

        if not role_name:
            return None

        r_clean = str(
            role_name
        ).strip()

        role_obj = (
            Role.objects
            .filter(
                name__iexact=r_clean
            )
            .first()
            or Role.objects.filter(
                role_id__iexact=r_clean
            ).first()
            or Role.objects.filter(
                name__icontains=r_clean
            ).first()
        )

        if not role_obj:

            lower = r_clean.lower()

            if (
                "monitor" in lower
                or "controller" in lower
            ):
                primary = "monitor"

            elif (
                "hmo" in lower
                or "insurance" in lower
            ):
                primary = "hmo"

            elif (
                "cash" in lower
                or "billing" in lower
            ):
                primary = "cashdesk"

            elif (
                "analytics" in lower
                or "executive" in lower
            ):
                primary = "analytics"

            else:
                primary = "helpdesk"

            role_obj = Role.objects.create(
                role_id=(
                    f"role-{int(time.time() * 1000)}"
                ),
                name=r_clean,
                description=(
                    f"Custom role: {r_clean}"
                ),
                primary_desk=primary,
                allowed_desks=[primary],
                is_system_role=False,
                status=True
            )

        return role_obj

    # --------------------------------------------------------
    # UPDATE
    # --------------------------------------------------------

    def update(
        self,
        instance,
        validated_data
    ):

        initial_data = (
            self.initial_data
            or {}
        )

        email = (
            validated_data.get("email")
            or initial_data.get("email")
            or ""
        ).strip().lower()

        if email:

            instance.email = email
            instance.username = email

        name = (
            validated_data.get("first_name")
            or initial_data.get("name")
        )

        if name:
            instance.first_name = name

        password = (
            validated_data.get("password")
            or initial_data.get("password")
        )

        if password and not (
            password.startswith("pbkdf2_")
            or password.startswith("argon2")
        ):

            instance.set_password(
                password
            )

        status_input = (
            initial_data.get("status")
        )

        if status_input:

            instance.is_active = (
                status_input
                != "Disabled"
            )

        instance.save()

        profile, _ = (
            UserProfile.objects
            .get_or_create(
                user=instance
            )
        )

        role_name = (
            initial_data.get("role")
        )

        if role_name:

            role_obj = (
                self._resolve_or_create_role(
                    role_name
                )
            )

            if role_obj:

                profile.role = role_obj
                profile.save()

        return instance


# ============================================================
# CUSTOM TIME SLOT SERIALIZER
# ============================================================

class CustomTimeSlotSerializer(
    serializers.ModelSerializer
):

    id = serializers.CharField(
        source="slot_id",
        required=False
    )

    slot_id = serializers.CharField(
        required=False
    )

    class Meta:
        model = CustomTimeSlot
        fields = [
            "id",
            "slot_id",
            "label",
            "created_at",
        ]

    def create(self, validated_data):

        slot_id = (
            validated_data.get("slot_id")
            or validated_data.get("id")
            or (
                f"slot-{int(time.time() * 1000)}-"
                f"{__import__('random').randint(100, 999)}"
            )
        )

        validated_data["slot_id"] = slot_id

        return super().create(
            validated_data
        )


# ============================================================
# ROLE SERIALIZER
# ============================================================

class RoleSerializer(
    serializers.ModelSerializer
):

    id = serializers.CharField(
        source="role_id",
        required=False
    )

    role_id = serializers.CharField(
        required=False
    )

    primary_desk = serializers.CharField(
        required=False,
        default="helpdesk"
    )

    primaryDesk = serializers.CharField(
        source="primary_desk",
        required=False
    )

    allowed_desks = serializers.JSONField(
        required=False,
        default=list
    )

    allowedDesks = serializers.JSONField(
        source="allowed_desks",
        required=False
    )

    is_system_role = serializers.BooleanField(
        required=False,
        default=False
    )

    isSystemRole = serializers.BooleanField(
        source="is_system_role",
        required=False
    )

    created_at = serializers.DateTimeField(
        required=False
    )

    createdAt = serializers.DateTimeField(
        source="created_at",
        required=False
    )

    status = serializers.BooleanField(
        required=False,
        default=True
    )

    class Meta:
        model = Role

        fields = [
            "id",
            "role_id",
            "name",
            "description",
            "primary_desk",
            "primaryDesk",
            "allowed_desks",
            "allowedDesks",
            "is_system_role",
            "isSystemRole",
            "status",
            "created_at",
            "createdAt",
        ]

    # --------------------------------------------------------
    # INPUT
    # --------------------------------------------------------

    def to_internal_value(self, data):

        data = (
            data.copy()
            if hasattr(data, "copy")
            else dict(data)
        )

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        status_val = data.get("status")

        if isinstance(
            status_val,
            str
        ):

            status_clean = (
                status_val
                .lower()
                .strip()
            )

            if status_clean in (
                "active",
                "true",
                "1",
                "enabled",
            ):

                data["status"] = True

            elif status_clean in (
                "disabled",
                "inactive",
                "false",
                "0",
            ):

                data["status"] = False

        elif status_val is None:

            data["status"] = True

        # ----------------------------------------------------
        # CAMEL CASE
        # ----------------------------------------------------

        if (
            "primaryDesk" in data
            and "primary_desk" not in data
        ):
            data["primary_desk"] = (
                data["primaryDesk"]
            )

        if (
            "allowedDesks" in data
            and "allowed_desks" not in data
        ):
            data["allowed_desks"] = (
                data["allowedDesks"]
            )

        if (
            "isSystemRole" in data
            and "is_system_role" not in data
        ):
            data["is_system_role"] = (
                data["isSystemRole"]
            )

        return super().to_internal_value(
            data
        )

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    def to_representation(self, instance):

        ret = super().to_representation(
            instance
        )

        ret["id"] = instance.role_id
        ret["role_id"] = instance.role_id

        ret["primaryDesk"] = (
            instance.primary_desk
        )

        ret["primary_desk"] = (
            instance.primary_desk
        )

        ret["allowedDesks"] = (
            instance.allowed_desks
            or []
        )

        ret["allowed_desks"] = (
            instance.allowed_desks
            or []
        )

        ret["isSystemRole"] = (
            instance.is_system_role
        )

        ret["is_system_role"] = (
            instance.is_system_role
        )

        ret["status"] = (
            "Active"
            if instance.status
            else "Disabled"
        )

        return ret

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    def create(self, validated_data):

        role_id = (
            validated_data.get("role_id")
            or validated_data.get("id")
            or (
                f"role-{int(time.time() * 1000)}-"
                f"{__import__('random').randint(100, 999)}"
            )
        )

        validated_data["role_id"] = role_id

        return super().create(
            validated_data
        )


# ============================================================
# APP SETTING SERIALIZER
# ============================================================

class AppSettingSerializer(
    serializers.ModelSerializer
):

    class Meta:
        model = AppSetting
        fields = "__all__"
