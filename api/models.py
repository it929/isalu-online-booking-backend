from django.db import models
from django.utils import timezone

# class Department(models.Model):
#     dept_id = models.CharField(max_length=50, primary_key=True)
#     name = models.CharField(max_length=200)
#     description = models.TextField(blank=True, default='')
#     icon_name = models.CharField(max_length=100, default='Stethoscope')
#     doctor_count = models.IntegerField(default=0)
#     location = models.CharField(max_length=200, blank=True, default='Main Building')
#     status = models.BooleanField(default=True)

#     class Meta:
#         ordering = ['name']

#     def __str__(self):
#         return f"{self.name} ({self.dept_id})"


# class Doctor(models.Model):
#     doc_id = models.CharField(max_length=100, primary_key=True)

#     name = models.CharField(
#         max_length=200,
#         help_text="Public display name / acronym (e.g. Specialist A)"
#     )

#     full_name = models.CharField(
#         max_length=200,
#         blank=True,
#         default='',
#         help_text="Full real name for Admin"
#     )

#     acronym = models.CharField(
#         max_length=50,
#         blank=True,
#         default=''
#     )

#     specialty = models.CharField(max_length=200)

#     department = models.ForeignKey(
#         Department,
#         on_delete=models.SET_NULL,
#         null=True,
#         blank=True,
#         related_name='doctors'
#     )

#     qualification = models.CharField(
#         max_length=250,
#         default='MBBS, FWACS'
#     )

#     qualifications = models.CharField(
#         max_length=250,
#         default='MBBS, FWACS'
#     )

#     image = models.TextField(
#         blank=True,
#         default=''
#     )

#     bio = models.TextField(
#         blank=True,
#         default='Senior Medical Consultant specializing in high-quality clinical care at Isalu Hospitals.'
#     )

#     accepted_patient_types = models.JSONField(
#         default=list,
#         blank=True
#     )

#     status = models.BooleanField(default=True)

#     class Meta:
#         ordering = ['doc_id']

#     def save(self, *args, **kwargs):
#         if self.department:
#             self.specialty = self.department.name

#         super().save(*args, **kwargs)

#     @property
#     def _prefetched_schedules(self):
#         if hasattr(
#             self,
#             "_prefetched_objects_cache"
#         ) and "schedules" in self._prefetched_objects_cache:

#             return self._prefetched_objects_cache["schedules"]

#         return self.schedules.all()

#     @property
#     def available_days(self):

#         schedules = self._prefetched_schedules

#         schedule = schedules[0] if schedules else None

#         if not schedule:
#             return []

#         return schedule.duty_days or []

#     @property
#     def time_slots(self):

#         schedules = self._prefetched_schedules

#         schedule = schedules[0] if schedules else None

#         if not schedule:
#             return []

#         if schedule.day_configs:
#             return [
#                 config.get("time")
#                 for config in schedule.day_configs.values()
#                 if isinstance(config, dict)
#                 and config.get("time")
#             ]

#         return (
#             [schedule.shift_time]
#             if schedule.shift_time
#             else []
#         )

#     @property
#     def room_number(self):

#         schedules = self._prefetched_schedules

#         schedule = schedules[0] if schedules else None

#         return (
#             schedule.room
#             if schedule
#             else ""
#         )

#     def __str__(self):
#         return f"{self.full_name or self.name} - {self.acronym or self.name}"


# class SpecialistSchedule(models.Model):
#     sched_id = models.CharField(max_length=100, primary_key=True)
#     doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, null=True, blank=True, related_name='schedules')
#     doctor_name = models.CharField(max_length=200, blank=True, default='Unassigned Doctor')
#     specialty = models.CharField(max_length=200, blank=True, default='General Medicine')
#     room = models.CharField(max_length=200, default='Consultation Suite')
#     duty_days = models.JSONField(default=list)
#     day_configs = models.JSONField(default=dict, blank=True)
#     shift_time = models.TextField(blank=True, default='08:00 AM – 02:00 PM')
#     capacity = models.IntegerField(default=15)
#     total_weekly_capacity = models.IntegerField(default=15)
#     status = models.BooleanField(default=True)

#     class Meta:
#         ordering = ['sched_id']

#     def save(self, *args, **kwargs):
#         if self.doctor:
#             if not self.doctor_name or self.doctor_name == 'Unassigned Doctor':
#                 self.doctor_name = self.doctor.full_name or self.doctor.name
#             if not self.specialty or self.specialty == 'General Medicine':
#                 self.specialty = self.doctor.department.name if self.doctor.department else (self.doctor.specialty or 'General Medicine')
#         super().save(*args, **kwargs)

#     def __str__(self):
#         return f"{self.doctor_name} ({self.shift_time})"


# class Booking(models.Model):
#     ref_code = models.CharField(
#         max_length=20,
#         unique=True,
#         null=False,
#         blank=False,
#         db_index=True
#     )
#     doctor_id = models.CharField(max_length=100)
#     doctor_name = models.CharField(max_length=200)
#     doctor_specialty = models.CharField(max_length=200)
#     date = models.CharField(max_length=20)
#     time = models.CharField(max_length=100)
#     patient_name = models.CharField(max_length=200)
#     patient_phone = models.CharField(max_length=50)
#     patient_email = models.EmailField(blank=True, default='')
#     reason = models.TextField(blank=True, default='')
#     payment_type = models.CharField(max_length=100, default='Private Self-Pay')
#     hmo_name = models.CharField(max_length=200, blank=True, default='N/A')
#     hmo_policy_code = models.CharField(max_length=100, blank=True, default='')
#     hmo_auth_code = models.CharField(max_length=100, blank=True, default='')
#     referral_doc_name = models.CharField(max_length=200, blank=True, default='')
#     referral_doc_data = models.TextField(blank=True, default='')
#     referral_doc_text = models.TextField(blank=True, default='')
#     hmo_status = models.TextField(blank=True, default='N/A')
#     payment_status = models.CharField(max_length=100, default='Pending')
#     payment_method = models.CharField(max_length=100, blank=True, default='POS / Cash')
#     invoice_ref = models.CharField(max_length=100, blank=True, default='')
#     status = models.CharField(max_length=100, default='Confirmed')
#     is_active = models.BooleanField(default=True)
#     delete_reason = models.TextField(blank=True, default='')
#     created_at = models.DateTimeField(default=timezone.now)

#     class Meta:
#         ordering = ['-created_at']

#     def __str__(self):
#         return f"Ticket {self.ref_code} - {self.patient_name}"


class HmoCompany(models.Model):
    hmo_id = models.CharField(max_length=100, primary_key=True)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    contact_person = models.CharField(max_length=200)
    status = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"


from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User


class Role(models.Model):
    role_id = models.CharField(max_length=100, primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True, default='')
    primary_desk = models.CharField(max_length=100, default='helpdesk')
    allowed_desks = models.JSONField(default=list, blank=True)
    is_system_role = models.BooleanField(default=False)
    status = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.primary_desk})"


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True, related_name='profile')
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True, related_name='user_profiles')

    class Meta:
        ordering = ['user__first_name', 'user__username']

    def __str__(self):
        return f"{self.user.username} - {self.role.name if self.role else 'No Role'}"



class CustomTimeSlot(models.Model):
    slot_id = models.CharField(max_length=100, primary_key=True)
    label = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.label



class AppSetting(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return self.key


class Department(models.Model):
    dept_id = models.CharField(
        max_length=50,
        primary_key=True,
    )

    name = models.CharField(
        max_length=200,
    )

    description = models.TextField(
        blank=True,
        default="",
    )

    icon_name = models.CharField(
        max_length=100,
        default="Stethoscope",
    )

    doctor_count = models.PositiveIntegerField(
        default=0,
    )

    location = models.CharField(
        max_length=200,
        blank=True,
        default="Main Building",
    )

    status = models.BooleanField(
        default=True,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.dept_id})"


# ============================================================
# DOCTOR
# ============================================================

class Doctor(models.Model):
    doc_id = models.CharField(
        max_length=100,
        primary_key=True,
    )

    name = models.CharField(
        max_length=200,
        help_text="Public display name / acronym (e.g. Specialist A)",
    )

    full_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Full real name for Admin",
    )

    acronym = models.CharField(
        max_length=50,
        blank=True,
        default="",
    )

    specialty = models.CharField(
        max_length=200,
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="doctors",
    )

    qualification = models.CharField(
        max_length=250,
        default="MBBS, FWACS",
    )

    qualifications = models.CharField(
        max_length=250,
        default="MBBS, FWACS",
    )

    image = models.TextField(
        blank=True,
        default="",
    )

    bio = models.TextField(
        blank=True,
        default=(
            "Senior Medical Consultant specializing in "
            "high-quality clinical care at Isalu Hospitals."
        ),
    )

    accepted_patient_types = models.JSONField(
        default=list,
        blank=True,
    )

    status = models.BooleanField(
        default=True,
    )

    class Meta:
        ordering = ["doc_id"]

    def save(self, *args, **kwargs):
        if self.department:
            self.specialty = self.department.name

        super().save(*args, **kwargs)

    @property
    def _prefetched_schedules(self):
        if (
            hasattr(self, "_prefetched_objects_cache")
            and "schedules" in self._prefetched_objects_cache
        ):
            return self._prefetched_objects_cache["schedules"]

        return self.schedules.all()

    @property
    def active_schedule(self):
        schedules = self._prefetched_schedules

        for schedule in schedules:
            if schedule.status:
                return schedule

        return schedules[0] if schedules else None

    @property
    def available_days(self):
        schedule = self.active_schedule

        if not schedule:
            return []

        return schedule.duty_days or []

    @property
    def time_slots(self):
        schedule = self.active_schedule

        if not schedule:
            return []

        if schedule.day_configs:
            slots = []

            for config in schedule.day_configs.values():
                if isinstance(config, dict):
                    time_value = config.get("time")

                    if time_value:
                        slots.append(str(time_value))

            return slots

        return (
            [schedule.shift_time]
            if schedule.shift_time
            else []
        )

    @property
    def room_number(self):
        schedule = self.active_schedule

        return schedule.room if schedule else ""

    @property
    def daily_capacity(self):
        schedule = self.active_schedule

        if not schedule:
            return 0

        return max(int(schedule.capacity or 0), 0)

    def __str__(self):
        return (
            f"{self.full_name or self.name} - "
            f"{self.acronym or self.name}"
        )


# ============================================================
# SPECIALIST SCHEDULE
# ============================================================

class SpecialistSchedule(models.Model):
    sched_id = models.CharField(
        max_length=100,
        primary_key=True,
    )

    doctor = models.ForeignKey(
        Doctor,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="schedules",
    )

    doctor_name = models.CharField(
        max_length=200,
        blank=True,
        default="Unassigned Doctor",
    )

    specialty = models.CharField(
        max_length=200,
        blank=True,
        default="General Medicine",
    )

    room = models.CharField(
        max_length=200,
        default="Consultation Suite",
    )

    duty_days = models.JSONField(
        default=list,
    )

    day_configs = models.JSONField(
        default=dict,
        blank=True,
    )

    shift_time = models.TextField(
        blank=True,
        default="08:00 AM – 02:00 PM",
    )

    # ========================================================
    # DAILY APPOINTMENT CAPACITY
    # ========================================================
    capacity = models.PositiveIntegerField(
        default=15,
        help_text=(
            "Maximum number of active appointments this "
            "doctor can accept per day."
        ),
    )

    total_weekly_capacity = models.PositiveIntegerField(
        default=15,
    )

    status = models.BooleanField(
        default=True,
    )

    class Meta:
        ordering = ["sched_id"]

    def save(self, *args, **kwargs):
        if self.doctor:
            if (
                not self.doctor_name
                or self.doctor_name == "Unassigned Doctor"
            ):
                self.doctor_name = (
                    self.doctor.full_name
                    or self.doctor.name
                )

            if (
                not self.specialty
                or self.specialty == "General Medicine"
            ):
                self.specialty = (
                    self.doctor.department.name
                    if self.doctor.department
                    else (
                        self.doctor.specialty
                        or "General Medicine"
                    )
                )

        self.capacity = max(int(self.capacity or 0), 0)
        self.total_weekly_capacity = max(
            int(self.total_weekly_capacity or 0),
            0,
        )

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.doctor_name} ({self.shift_time})"


# ============================================================
# BOOKING
# ============================================================

class Booking(models.Model):
    ref_code = models.CharField(
        max_length=20,
        primary_key=True,
        null=False,
        blank=False,
    )

    doctor_id = models.CharField(
        max_length=100,
        db_index=True,
    )

    doctor_name = models.CharField(
        max_length=200,
    )

    doctor_specialty = models.CharField(
        max_length=200,
    )

    # Kept as CharField for backwards compatibility with
    # your existing frontend/API.
    date = models.CharField(
        max_length=20,
        db_index=True,
    )

    time = models.CharField(
        max_length=100,
    )

    patient_name = models.CharField(
        max_length=200,
    )

    patient_phone = models.CharField(
        max_length=50,
    )

    patient_email = models.EmailField(
        blank=True,
        default="",
    )

    reason = models.TextField(
        blank=True,
        default="",
    )

    payment_type = models.CharField(
        max_length=100,
        default="Private Self-Pay",
    )

    hmo_name = models.CharField(
        max_length=200,
        blank=True,
        default="N/A",
    )

    hmo_policy_code = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    hmo_auth_code = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    referral_doc_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    referral_doc_data = models.TextField(
        blank=True,
        default="",
    )

    referral_doc_text = models.TextField(
        blank=True,
        default="",
    )

    hmo_status = models.TextField(
        blank=True,
        default="N/A",
    )

    payment_status = models.CharField(
        max_length=100,
        default="Pending",
    )

    payment_method = models.CharField(
        max_length=100,
        blank=True,
        default="POS / Cash",
    )

    invoice_ref = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    status = models.CharField(
        max_length=100,
        default="Confirmed",
        db_index=True,
    )

    is_active = models.BooleanField(
        default=True,
        db_index=True,
    )

    delete_reason = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]

        indexes = [
            models.Index(
                fields=["doctor_id", "date"],
                name="booking_doctor_date_idx",
            ),
            models.Index(
                fields=["doctor_id", "date", "is_active"],
                name="booking_doctor_active_idx",
            ),
            models.Index(
                fields=["doctor_id", "date", "time"],
                name="booking_doctor_time_idx",
            ),
        ]

    def __str__(self):
        return (
            f"Ticket {self.ref_code} - "
            f"{self.patient_name}"
        )

    # ========================================================
    # BOOKING STATUS HELPERS
    # ========================================================

    @property
    def counts_toward_capacity(self):
        """
        Determines whether this booking should consume
        the doctor's daily appointment capacity.

        Cancelled, rejected, deleted and inactive bookings
        do not consume capacity.
        """

        if not self.is_active:
            return False

        status = str(
            self.status or ""
        ).strip().lower()

        excluded_statuses = {
            "cancelled",
            "canceled",
            "rejected",
            "declined",
            "deleted",
            "expired",
            "void",
        }

        return status not in excluded_statuses
