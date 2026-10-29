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


class Company(models.Model):
    """
    A company whose staff are booked as "Company" patients. Works like an HMO
    (the company covers the visit and the HMO desk approves it) but patients
    don't need an enrollee ID. Managed in the dashboard's Companies module.
    """
    company_id = models.CharField(max_length=100, primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    code = models.CharField(max_length=50, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=50, blank=True, default='')
    contact_person = models.CharField(max_length=200, blank=True, default='')
    address = models.CharField(max_length=300, blank=True, default='')
    status = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'companies'

    def __str__(self):
        return self.name


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

    # Consultation fee in Naira that private self-pay patients pay online
    # (Paystack) when booking this clinic. 0 = no online payment; the
    # patient pays at the cashdesk as before.
    consultation_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    # Follow-up visit fee in Naira. consultation_fee above is the first-visit
    # fee. 0 = follow-ups cost the same as a first visit.
    follow_up_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
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

    # HMO desk decision when pre-authorization is refused.
    hmo_decline_reason = models.TextField(
        blank=True,
        default="",
    )

    hmo_declined_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    hmo_declined_by = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    # ---- Online payment (Paystack), private self-pay only -------------
    # A booking waiting for payment holds its slot until payment_expires_at.
    fee_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Clinic fee charged online when the booking was made (Naira).",
    )

    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
    )

    payment_expires_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    amount_paid = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    # "First Visit" or "Follow-up". A follow-up is charged the clinic's
    # follow-up fee; if the hospital finds on arrival that it isn't a
    # follow-up, the patient pays the balance up to the first-visit fee
    # (first_visit_fee, recorded at booking time). The patient agrees to this
    # on the booking form (follow_up_acknowledged).
    # Optional hospital card number the patient typed on the booking form.
    hospital_card_number = models.CharField(max_length=30, blank=True, default="", db_index=True)

    # Booked by someone else (a relative, carer, employer...): their contact details.
    # The patient's own name and phone stay in patient_name / patient_phone.
    booked_for_other = models.BooleanField(default=False)
    booker_name = models.CharField(max_length=200, blank=True, default="")
    booker_phone = models.CharField(max_length=30, blank=True, default="")
    booker_email = models.EmailField(blank=True, default="")

    # When the patient was sent their payment receipt (SMS/email), so it goes once.
    payment_receipt_sent_at = models.DateTimeField(null=True, blank=True)

    visit_type = models.CharField(max_length=20, default="First Visit", blank=True)
    follow_up_acknowledged = models.BooleanField(default=False)
    first_visit_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

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

        from .scheduling import AWAITING_PAYMENT, INACTIVE_BOOKING_STATUSES

        status = str(
            self.status or ""
        ).strip().lower()

        if status == AWAITING_PAYMENT.lower():
            # An unpaid online booking holds its slot only until the hold expires.
            return bool(self.payment_expires_at and self.payment_expires_at > timezone.now())

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


class PaymentCheck(models.Model):
    """
    Payment Reconciliation log: every time staff check an online payment
    reference against Paystack (and every booking they confirm from it).
    """

    ACTION_CHECK = "check"
    ACTION_CONFIRM = "confirm"

    reference = models.CharField(max_length=100, db_index=True)
    ref_code = models.CharField(max_length=20, blank=True, default="", db_index=True)
    patient_name = models.CharField(max_length=200, blank=True, default="")
    action = models.CharField(max_length=10, default=ACTION_CHECK)
    paystack_status = models.CharField(max_length=30, blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    verdict = models.CharField(max_length=40, blank=True, default="")
    note = models.CharField(max_length=300, blank=True, default="")
    checked_by = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} {self.action} {self.verdict} by {self.checked_by}"



class PaymentAttempt(models.Model):
    """
    Every Paystack transaction the booking site opens. A patient who presses Pay
    twice has two attempts; a late payment on the older one is still matched to
    the booking through this table.
    """

    reference = models.CharField(max_length=100, unique=True)
    booking_ref = models.CharField(max_length=20, db_index=True)
    amount_kobo = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=5, default="NGN")
    # initialized, success, failed, abandoned, reversed, pending, ...
    status = models.CharField(max_length=20, default="initialized", db_index=True)
    paystack_id = models.CharField(max_length=40, blank=True, default="")
    paid_kobo = models.BigIntegerField(null=True, blank=True)
    fees_kobo = models.BigIntegerField(null=True, blank=True)
    channel = models.CharField(max_length=40, blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} ({self.booking_ref}) {self.status}"


class PaymentEvent(models.Model):
    """
    Append-only money ledger: confirmations, duplicate and short payments,
    reversals, disputes, refunds and staff actions, with who/what caused them.
    Never edited or deleted (the admin is read-only).
    """

    KINDS = [
        ("payment_confirmed", "Payment confirmed"),
        ("duplicate_payment", "Duplicate payment"),
        ("underpaid", "Paid less than the fee"),
        ("paid_clinic_full", "Paid after the clinic filled"),
        ("unmatched_payment", "Payment with no booking"),
        ("payment_reversed", "Payment reversed"),
        ("dispute_opened", "Dispute opened"),
        ("dispute_won", "Dispute resolved for the hospital"),
        ("dispute_lost", "Dispute resolved for the patient"),
        ("refund_requested", "Refund started"),
        ("refund_processed", "Refund completed"),
        ("refund_failed", "Refund failed"),
        ("receipt_sent", "Patient notified"),
        ("cleared_without_payment", "Cleared but Paystack shows no payment"),
        ("reschedule_paid", "Reschedule fee paid"),
        ("reschedule_not_applied", "Reschedule paid, new date no longer free"),
    ]

    kind = models.CharField(max_length=40, choices=KINDS, db_index=True)
    reference = models.CharField(max_length=100, blank=True, default="", db_index=True)
    ref_code = models.CharField(max_length=20, blank=True, default="", db_index=True)
    patient_name = models.CharField(max_length=200, blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # verify (patient's browser), webhook, sweep (automatic check), staff, system
    source = models.CharField(max_length=20, default="system")
    actor = models.CharField(max_length=200, blank=True, default="")
    ip = models.CharField(max_length=60, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.kind} {self.reference}"


class Refund(models.Model):
    """Money owed back to a patient, and how it was paid back."""

    STATUS_DUE = "due"              # owed, nothing sent yet
    STATUS_PENDING = "pending"      # sent to Paystack, waiting for the bank
    STATUS_PROCESSED = "processed"  # paid back
    STATUS_FAILED = "failed"        # Paystack couldn't refund; try again or pay manually
    STATUS_CANCELLED = "cancelled"  # not owed after all (e.g. booked another date instead)
    OPEN_STATUSES = (STATUS_DUE, STATUS_PENDING, STATUS_FAILED)

    reference = models.CharField(max_length=100, db_index=True)   # the Paystack payment being refunded
    ref_code = models.CharField(max_length=20, blank=True, default="", db_index=True)
    patient_name = models.CharField(max_length=200, blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # duplicate, clinic_full, underpaid, other
    reason = models.CharField(max_length=30, default="other")
    status = models.CharField(max_length=20, default=STATUS_DUE, db_index=True)
    # paystack, bank_transfer, cash ("" while due)
    method = models.CharField(max_length=20, blank=True, default="")
    paystack_refund_id = models.CharField(max_length=40, blank=True, default="", db_index=True)
    note = models.CharField(max_length=500, blank=True, default="")
    last_error = models.CharField(max_length=500, blank=True, default="")
    created_by = models.CharField(max_length=200, blank=True, default="")
    processed_by = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Refund {self.amount} for {self.reference} ({self.status})"


class WebhookEvent(models.Model):
    """Each Paystack webhook received once (replays are ignored)."""

    key = models.CharField(max_length=64, unique=True)   # sha256 of the signed body
    event = models.CharField(max_length=60, blank=True, default="")
    reference = models.CharField(max_length=100, blank=True, default="", db_index=True)
    result = models.CharField(max_length=200, blank=True, default="")
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-received_at"]


class PaymentJobState(models.Model):
    """Lock and last result of the automatic payment check."""

    key = models.CharField(max_length=40, primary_key=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.JSONField(default=dict, blank=True)



class ReschedulePayment(models.Model):
    """
    A patient's online reschedule: the new date/time they chose and the
    reschedule service charge they pay with Paystack. The booking moves only
    once Paystack confirms the payment.
    """

    STATUS_INITIALIZED = "initialized"   # checkout opened, not paid yet
    STATUS_APPLIED = "applied"           # paid and the booking moved
    STATUS_NOT_APPLIED = "not_applied"   # paid, but the new date was no longer free (refund opened)
    STATUS_FAILED = "failed"             # paid less than the charge (refund opened)
    STATUS_REPLACED = "replaced"         # the patient started another reschedule instead

    reference = models.CharField(max_length=100, unique=True)
    booking_ref = models.CharField(max_length=20, db_index=True)
    old_date = models.CharField(max_length=20, blank=True, default="")
    old_time = models.CharField(max_length=100, blank=True, default="")
    new_date = models.CharField(max_length=20)
    new_time = models.CharField(max_length=100, blank=True, default="")
    new_doctor_id = models.CharField(max_length=100, blank=True, default="")
    reason = models.CharField(max_length=300, blank=True, default="")
    base_fee = models.DecimalField(max_digits=12, decimal_places=2)
    percent = models.PositiveIntegerField(default=25)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default=STATUS_INITIALIZED, db_index=True)
    note = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Reschedule {self.booking_ref} -> {self.new_date} ({self.status})"
