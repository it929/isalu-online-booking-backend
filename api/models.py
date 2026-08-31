from django.db import models
from django.utils import timezone

class Department(models.Model):
    dept_id = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    icon_name = models.CharField(max_length=100, default='Stethoscope')
    doctor_count = models.IntegerField(default=0)
    location = models.CharField(max_length=200, blank=True, default='Main Building')
    status = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.dept_id})"


class Doctor(models.Model):
    doc_id = models.CharField(max_length=100, primary_key=True)
    name = models.CharField(max_length=200, help_text="Public display name / acronym (e.g. Specialist A)")
    full_name = models.CharField(max_length=200, blank=True, default='', help_text="Full real name for Admin (e.g. Dr. Adewale Olusola)")
    acronym = models.CharField(max_length=50, blank=True, default='', help_text="Saved acronym (e.g. Specialist A)")
    specialty = models.CharField(max_length=200)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True, related_name='doctors')
    qualification = models.CharField(max_length=250, default='MBBS, FWACS')
    qualifications = models.CharField(max_length=250, default='MBBS, FWACS')
    image = models.TextField(blank=True, default='')
    bio = models.TextField(blank=True, default='Senior Medical Consultant specializing in high-quality clinical care at Isalu Hospitals.')
    accepted_patient_types = models.JSONField(default=list, blank=True, help_text="Accepted patient categories e.g. ['Private Self-Pay', 'HMO Insurance']")
    status = models.BooleanField(default=True)

    class Meta:
        ordering = ['doc_id']

    def save(self, *args, **kwargs):
        if self.department:
            self.specialty = self.department.name
        super().save(*args, **kwargs)

    @property
    def available_days(self):
        days = []
        for s in self.schedules.all():
            for d in (s.duty_days or []):
                if d not in days:
                    days.append(d)
        return days

    @property
    def time_slots(self):
        slots = []
        for s in self.schedules.all():
            if s.shift_time and s.shift_time not in slots:
                slots.append(s.shift_time)
        return slots

    @property
    def room_number(self):
        sched = self.schedules.first()
        return sched.room if (sched and sched.room) else "Consultation Suite 4B"

    def __str__(self):
        return f"{self.full_name or self.name} - {self.acronym or self.name}"


class SpecialistSchedule(models.Model):
    sched_id = models.CharField(max_length=100, primary_key=True)
    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, null=True, blank=True, related_name='schedules')
    doctor_name = models.CharField(max_length=200, blank=True, default='Unassigned Doctor')
    specialty = models.CharField(max_length=200, blank=True, default='General Medicine')
    room = models.CharField(max_length=200, default='Consultation Suite')
    duty_days = models.JSONField(default=list)
    day_configs = models.JSONField(default=dict, blank=True)
    shift_time = models.TextField(blank=True, default='08:00 AM – 02:00 PM')
    capacity = models.IntegerField(default=15)
    total_weekly_capacity = models.IntegerField(default=15)
    status = models.BooleanField(default=True)

    class Meta:
        ordering = ['sched_id']

    def save(self, *args, **kwargs):
        if self.doctor:
            if not self.doctor_name or self.doctor_name == 'Unassigned Doctor':
                self.doctor_name = self.doctor.full_name or self.doctor.name
            if not self.specialty or self.specialty == 'General Medicine':
                self.specialty = self.doctor.department.name if self.doctor.department else (self.doctor.specialty or 'General Medicine')
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.doctor_name} ({self.shift_time})"


class Booking(models.Model):
    ref_code = models.CharField(max_length=50, primary_key=True)
    doctor_id = models.CharField(max_length=100)
    doctor_name = models.CharField(max_length=200)
    doctor_specialty = models.CharField(max_length=200)
    date = models.CharField(max_length=20)
    time = models.CharField(max_length=100)
    patient_name = models.CharField(max_length=200)
    patient_phone = models.CharField(max_length=50)
    patient_email = models.EmailField(blank=True, default='')
    reason = models.TextField(blank=True, default='')
    payment_type = models.CharField(max_length=100, default='Private Self-Pay')
    hmo_name = models.CharField(max_length=200, blank=True, default='N/A')
    hmo_policy_code = models.CharField(max_length=100, blank=True, default='')
    hmo_auth_code = models.CharField(max_length=100, blank=True, default='')
    referral_doc_name = models.CharField(max_length=200, blank=True, default='')
    hmo_status = models.TextField(blank=True, default='N/A')
    payment_status = models.CharField(max_length=100, default='Pending')
    payment_method = models.CharField(max_length=100, blank=True, default='POS / Cash')
    invoice_ref = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=100, default='Confirmed')
    is_active = models.BooleanField(default=True)
    delete_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Ticket {self.ref_code} - {self.patient_name}"


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


