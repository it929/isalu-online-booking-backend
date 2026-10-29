"""
Double-booking guard.

Rule: the same patient - same full name AND same phone number - may hold only
ONE active appointment per clinic (department) on the same day. Booking the
same clinic on a different day is allowed. A cancelled or completed visit
frees the day again.

- Names match ignoring case, extra spaces, punctuation and common titles
  ("Mrs. Ada  Obi" == "ada obi").
- Phones match in any common format ("0801 234 5678" == "+2348012345678").
- A different name on the same phone (e.g. a parent booking for a child) is
  allowed, as is the same name with a different phone.
"""

import re


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


def _iso_date(value):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value).strip()[:10] or None


def find_duplicate_booking(doctor, patient_name, patient_phone, exclude_ref=None, date=None):
    """
    Return the patient's existing active booking in the same clinic on the
    same day (``date``), or None. Without a date nothing can clash yet, so
    None is returned.
    """
    day = _iso_date(date)
    if not day:
        return None
    name_key = normalize_name(patient_name)
    phone_key = normalize_phone(patient_phone)
    if not name_key or len(phone_key) < 7 or doctor is None:
        return None

    label, doctor_ids, specialty = clinic_scope(doctor)
    qs = capacity_bookings_qs().filter(date=day, doctor_id__in=doctor_ids)
    if specialty:
        qs = qs | capacity_bookings_qs().filter(date=day, doctor_specialty__iexact=specialty)
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
        f"Double booking: {existing.patient_name} with phone {existing.patient_phone} already has a "
        f"{clinic_label} appointment on {existing.date} (ref {existing.ref_code}). "
        "A patient can have only one appointment in a clinic on the same day. Please choose "
        "another date, or use 'Check Appointment' to change the existing booking."
    )


def release_other_payment_holds(doctor, patient_name, patient_phone, keep_ref):
    """
    A patient who goes back and picks another date starts a new online
    payment. Their earlier unpaid holds in the same clinic are released so
    those slots aren't blocked until the hold times out. Paid bookings are
    never touched.
    """
    from django.utils import timezone

    from .models import Booking
    from .scheduling import AWAITING_PAYMENT, PAYMENT_EXPIRED

    name_key = normalize_name(patient_name)
    phone_key = normalize_phone(patient_phone)
    if not name_key or len(phone_key) < 7 or doctor is None:
        return 0
    _label, doctor_ids, specialty = clinic_scope(doctor)
    qs = Booking.objects.filter(status__iexact=AWAITING_PAYMENT, doctor_id__in=doctor_ids)
    if specialty:
        qs = qs | Booking.objects.filter(status__iexact=AWAITING_PAYMENT, doctor_specialty__iexact=specialty)
    refs = [
        b.ref_code
        for b in qs.exclude(ref_code=keep_ref).only("ref_code", "patient_name", "patient_phone")
        if normalize_phone(b.patient_phone) == phone_key and normalize_name(b.patient_name) == name_key
    ]
    if refs:
        Booking.objects.filter(ref_code__in=refs, status__iexact=AWAITING_PAYMENT).update(
            status=PAYMENT_EXPIRED,
            delete_reason="Replaced by a newer booking attempt from the same patient.",
            updated_at=timezone.now(),
        )
    return len(refs)


# ---------------------------------------------------------------------------
# Follow-up eligibility: a follow-up must be within a month of the patient's
# last appointment in the same clinic.
# ---------------------------------------------------------------------------

FOLLOW_UP_WINDOW_DAYS = 30
_NOT_A_VISIT = ("cancelled", "canceled", "disabled", "awaiting payment", "payment expired")


def last_clinic_visit(doctor, patient_name="", patient_phone="", card_number="", before=None, exclude_ref=None):
    """
    The patient's most recent earlier appointment in this doctor's clinic
    (any doctor in the same department), or None. The patient is the same
    name + phone (like the double-booking rule), or the same hospital card
    number when one is given. Only appointments dated before `before`
    (a date; default today) count.
    """
    import datetime as _dt
    from django.db.models import Q
    from django.utils import timezone
    from .models import Booking

    if doctor is None:
        return None
    name_key = normalize_name(patient_name)
    phone_key = normalize_phone(patient_phone)
    card_key = " ".join(str(card_number or "").split()).upper()
    if not card_key and (not name_key or len(phone_key) < 7):
        return None
    before = before or timezone.localdate()
    if isinstance(before, str):
        try:
            before = _dt.date.fromisoformat(before[:10])
        except ValueError:
            before = timezone.localdate()

    _, doctor_ids, specialty = clinic_scope(doctor)
    in_clinic = Q(doctor_id__in=doctor_ids)
    if specialty:
        in_clinic |= Q(doctor_specialty__iexact=specialty)
    qs = Booking.objects.filter(in_clinic, is_active=True, date__lt=before.isoformat())
    for s in _NOT_A_VISIT:
        qs = qs.exclude(status__iexact=s)
    if exclude_ref:
        qs = qs.exclude(ref_code=exclude_ref)
    # Narrow in the database (card, or the last 4 phone digits), compare exactly in Python.
    narrow = Q()
    if card_key:
        narrow |= Q(hospital_card_number__iexact=card_key)
    if len(phone_key) >= 7:
        narrow |= Q(patient_phone__contains=phone_key[-4:])
    qs = qs.filter(narrow).only("ref_code", "patient_name", "patient_phone", "hospital_card_number", "date", "doctor_specialty")
    for b in qs.order_by("-date")[:200]:
        same_card = card_key and (b.hospital_card_number or "").upper() == card_key
        same_person = name_key and normalize_name(b.patient_name) == name_key and normalize_phone(b.patient_phone) == phone_key
        if same_card or same_person:
            return b
    return None


def follow_up_locked(doctor, patient_name="", patient_phone="", card_number="", appointment_date=None, exclude_ref=None):
    """
    (locked, last_visit_date). Locked to the first-visit fee when the patient's
    last appointment in this clinic was more than FOLLOW_UP_WINDOW_DAYS before
    the new appointment. No earlier appointment on record: not locked (the
    hospital verifies on arrival, as before).
    """
    import datetime as _dt
    from django.utils import timezone

    when = appointment_date or timezone.localdate()
    if isinstance(when, str):
        try:
            when = _dt.date.fromisoformat(when[:10])
        except ValueError:
            when = timezone.localdate()
    today = timezone.localdate()
    last = last_clinic_visit(doctor, patient_name, patient_phone, card_number, before=min(when, today + _dt.timedelta(days=1)),
                             exclude_ref=exclude_ref)
    if last is None:
        return False, None
    try:
        last_day = _dt.date.fromisoformat(str(last.date)[:10])
    except ValueError:
        return False, None
    return (when - last_day).days > FOLLOW_UP_WINDOW_DAYS, last_day
