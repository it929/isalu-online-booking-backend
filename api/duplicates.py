"""
Double-booking guard.

Rule: the same patient - same full name AND same phone number - may hold only
ONE active upcoming appointment per clinic (department). They can book that
clinic again once the visit is completed or cancelled.

- Names match ignoring case, extra spaces, punctuation and common titles
  ("Mrs. Ada  Obi" == "ada obi").
- Phones match in any common format ("0801 234 5678" == "+2348012345678").
- A different name on the same phone (e.g. a parent booking for a child) is
  allowed, as is the same name with a different phone.
"""

import re

from django.utils import timezone

from .models import Doctor
from .scheduling import capacity_bookings_qs

_TITLES = {
    "mr", "mrs", "ms", "miss", "mstr", "master", "dr", "prof", "chief", "alhaji",
    "alhaja", "pastor", "rev", "engr", "barr", "hon", "sir", "madam",
}
# Statuses after which a patient may book the same clinic again.
_FINISHED = ("completed", "checked in")


def normalize_name(name):
    words = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower()).split()
    words = [w for w in words if w not in _TITLES]
    return " ".join(words)


def normalize_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("234") and len(digits) > 10:
        digits = digits[3:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits[-10:]


def clinic_scope(doctor):
    """(label, Q-able doctor ids, specialty name) describing the doctor's clinic."""
    if doctor.department_id:
        doctor_ids = list(
            Doctor.objects.filter(department_id=doctor.department_id).values_list("doc_id", flat=True)
        )
        return doctor.department.name, doctor_ids, doctor.department.name
    return (doctor.specialty or doctor.full_name or doctor.name), [doctor.doc_id], None


def find_duplicate_booking(doctor, patient_name, patient_phone, exclude_ref=None):
    """Return the existing active upcoming booking that would be duplicated, or None."""
    name_key = normalize_name(patient_name)
    phone_key = normalize_phone(patient_phone)
    if not name_key or len(phone_key) < 7 or doctor is None:
        return None

    label, doctor_ids, specialty = clinic_scope(doctor)
    today = timezone.localdate().isoformat()

    qs = capacity_bookings_qs().filter(date__gte=today, doctor_id__in=doctor_ids)
    if specialty:
        qs = qs | capacity_bookings_qs().filter(date__gte=today, doctor_specialty__iexact=specialty)
    for status in _FINISHED:
        qs = qs.exclude(status__iexact=status)
    if exclude_ref:
        qs = qs.exclude(ref_code=exclude_ref)

    # Compared in Python so every stored phone format matches.
    qs = qs.only(
        "ref_code", "patient_name", "patient_phone", "date", "time", "doctor_specialty"
    )
    for booking in qs.order_by("date"):
        if normalize_phone(booking.patient_phone) == phone_key and normalize_name(booking.patient_name) == name_key:
            booking.clinic_label = label
            return booking
    return None


def duplicate_message(existing, clinic_label):
    return (
        f"Double booking: {existing.patient_name} with phone {existing.patient_phone} already has an "
        f"upcoming {clinic_label} appointment on {existing.date} (ref {existing.ref_code}). "
        "Each patient can hold one upcoming appointment per clinic. To change the date, use "
        "'Check Appointment' to reschedule the existing booking."
    )
