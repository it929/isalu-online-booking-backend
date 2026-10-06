import datetime
from django.db import models
from django.utils import timezone


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

        slots = []
        configs = schedule.day_configs if isinstance(schedule.day_configs, dict) else {}

        for config in configs.values():
            if not isinstance(config, dict):
                continue
            times = (
                config.get("shiftTimes")
                or config.get("shift_times")
                or config.get("time")
            )
            if isinstance(times, str):
                times = [times]
            for value in times or []:
                value = str(value).strip()
                if value and value not in slots:
                    slots.append(value)

        if slots:
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
            return 15

        return max(int(schedule.capacity or 15), 0)

    def get_capacity_for_date(self, date_val=None):
        """
        Patient capacity for a specific date, honouring per-day overrides,
        nth-week recurrence and one-off dates. Returns 0 when the doctor
        is not on duty that day.
        """
        if date_val in (None, ""):
            return self.daily_capacity

        from .scheduling import resolve_doctor_day

        resolved = resolve_doctor_day(self, date_val)
        return resolved["capacity"] if resolved["on_duty"] else 0

    def resolve_day(self, date_val):
        """Full duty/capacity resolution for one date (see api.scheduling)."""
        from .scheduling import resolve_doctor_day

        return resolve_doctor_day(self, date_val)

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
        # Always mirror the linked doctor so the roster never shows a stale
        # name/specialty after the schedule is re-assigned.
        if self.doctor:
            self.doctor_name = (
                self.doctor.full_name
                or self.doctor.name
                or self.doctor_name
            )
            self.specialty = (
                self.doctor.department.name
                if self.doctor.department
                else (
                    self.doctor.specialty
                    or self.specialty
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

    reminder_sent = models.BooleanField(
        default=False,
        db_index=True,
    )

    reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    # Lets the dashboard download only bookings changed since its last sync.
    updated_at = models.DateTimeField(
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

    def save(self, *args, **kwargs):
        # Stamp every change, including save(update_fields=[...]) calls,
        # which would otherwise skip the timestamp.
        self.updated_at = timezone.now()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "updated_at" not in update_fields:
            kwargs["update_fields"] = list(update_fields) + ["updated_at"]
        super().save(*args, **kwargs)

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

        from .scheduling import INACTIVE_BOOKING_STATUSES

        status = str(
            self.status or ""
        ).strip().lower()

        return status not in INACTIVE_BOOKING_STATUSES


# ============================================================
# SCHEDULE EXCEPTION (one-off cancel / move of a clinic day)
# ============================================================

class ScheduleException(models.Model):
    """
    A one-off change to a doctor's regular clinic on a single date.

    cancel:     the clinic on `original_date` does not hold. Affected bookings
                are cancelled and patients are notified.
    reschedule: the clinic on `original_date` is held on `new_date` instead,
                for that occurrence only. Affected bookings move to `new_date`
                and patients are notified; the normal 3-hour reminder then
                fires for the new date.

    The doctor's regular schedule (SpecialistSchedule) is never modified.
    """

    ACTION_CANCEL = "cancel"
    ACTION_RESCHEDULE = "reschedule"
    ACTION_CHOICES = [
        (ACTION_CANCEL, "Cancel clinic"),
        (ACTION_RESCHEDULE, "Move clinic to another date"),
    ]

    exception_id = models.CharField(max_length=100, primary_key=True)

    doctor = models.ForeignKey(
        Doctor,
        on_delete=models.CASCADE,
        related_name="schedule_exceptions",
    )

    # Stored as YYYY-MM-DD strings to match Booking.date.
    original_date = models.CharField(max_length=10, db_index=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    new_date = models.CharField(max_length=10, blank=True, default="", db_index=True)

    # Clinic hours and capacity on new_date (reschedule only).
    shift_times = models.JSONField(default=list, blank=True)
    capacity = models.PositiveIntegerField(default=0)

    reason = models.TextField(blank=True, default="")

    affected_refs = models.JSONField(default=list, blank=True)
    affected_count = models.PositiveIntegerField(default=0)

    # pending -> sending -> sent | partial | failed | none
    notification_status = models.CharField(max_length=20, default="pending")
    notified_count = models.PositiveIntegerField(default=0)
    notification_failed_count = models.PositiveIntegerField(default=0)
    notification_log = models.JSONField(default=list, blank=True)

    created_by = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["original_date", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["doctor", "original_date"],
                name="unique_exception_per_doctor_date",
            ),
        ]

    def __str__(self):
        target = f" -> {self.new_date}" if self.action == self.ACTION_RESCHEDULE else ""
        return f"{self.doctor_id} {self.original_date} {self.action}{target}"
