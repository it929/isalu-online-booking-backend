from django.db import models
from django.utils import timezone

class Department(models.Model):
    dept_id = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    icon_name = models.CharField(max_length=100, default='Stethoscope')
    doctor_count = models.IntegerField(default=0)
    location = models.CharField(max_length=200, blank=True, default='Main Building')
    status = models.CharField(max_length=50, default='Active')

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
    department_id = models.CharField(max_length=100, default='pediatrics')
    qualification = models.CharField(max_length=250, default='MBBS, FWACS')
    qualifications = models.CharField(max_length=250, default='MBBS, FWACS')
    available_days = models.JSONField(default=list, blank=True)
    time_slots = models.JSONField(default=list, blank=True)
    image = models.TextField(blank=True, default='')
    bio = models.TextField(blank=True, default='Senior Medical Consultant specializing in high-quality clinical care at Isalu Hospitals.')
    room_number = models.CharField(max_length=100, default='Consultation Suite 4B')
    accepted_patient_types = models.JSONField(default=list, blank=True, help_text="Accepted patient categories e.g. ['Private Self-Pay', 'HMO Insurance']")
    status = models.CharField(max_length=50, default='Active')

    class Meta:
        ordering = ['doc_id']

    def __str__(self):
        return f"{self.full_name or self.name} - {self.acronym or self.name}"


class SpecialistSchedule(models.Model):
    sched_id = models.CharField(max_length=100, primary_key=True)
    doctor_id = models.CharField(max_length=100)
    doctor_name = models.CharField(max_length=200)
    specialty = models.CharField(max_length=200)
    room = models.CharField(max_length=200, default='Consultation Suite')
    duty_days = models.JSONField(default=list)
    day_configs = models.JSONField(default=dict, blank=True)
    shift_time = models.TextField(blank=True, default='08:00 AM – 02:00 PM')
    capacity = models.IntegerField(default=15)
    total_weekly_capacity = models.IntegerField(default=15)
    status = models.CharField(max_length=100, default='Active On Duty')

    class Meta:
        ordering = ['sched_id']

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
    status = models.CharField(max_length=100, default='Active Partner')

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"


from django.contrib.auth.hashers import make_password


class SystemUser(models.Model):
    user_id = models.CharField(max_length=100, primary_key=True)
    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255, default='admin123')
    role = models.CharField(max_length=100, default='Helpdesk Officer')
    desk = models.CharField(max_length=100, default='Helpdesk Reception')
    status = models.CharField(max_length=50, default='Active')
    last_active = models.CharField(max_length=100, default='Just now')

    class Meta:
        ordering = ['name']

    def save(self, *args, **kwargs):
        if self.password and not (
            self.password.startswith('pbkdf2_') or
            self.password.startswith('argon2') or
            self.password.startswith('bcrypt')
        ):
            self.password = make_password(self.password)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - {self.role}"


class CustomTimeSlot(models.Model):
    slot_id = models.CharField(max_length=100, primary_key=True)
    label = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.label
