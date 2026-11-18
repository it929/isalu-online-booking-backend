"""
Telemedicine API (see telemedicine.py for how it fits together).

Staff (Online Clinic desk "telemedicine", or administrators):
    GET    /api/online-sessions/?date_from=&date_to=&doctor_id=     list
    POST   /api/online-sessions/                 {doctor_id, date, start_time, end_time, slot_minutes, notes}
    PATCH  /api/online-sessions/<id>/            change hours / slot length (only before anyone books) or notes
    POST   /api/online-sessions/<id>/cancel/     only while nobody has booked
    GET    /api/online-sessions/<id>/queue/      session + patients in slot order, who is in the waiting room
    POST   /api/online-sessions/<id>/start/      go live (on the day)
    POST   /api/online-sessions/<id>/end/        end the online clinic
    POST   /api/online-sessions/<id>/admit/      {ref_code}  let a waiting patient in
    POST   /api/online-sessions/<id>/extend/     {ref_code, minutes: 5|10|15}
    POST   /api/online-sessions/<id>/end-call/   {ref_code}
    POST   /api/online-sessions/<id>/missed/     {ref_code}

Public:
    GET    /api/telemedicine/slots/?doctor_id=  or ?department=    bookable online clinics + slots
    GET    /api/telemedicine/join/<video_token>/                     the patient's consultation state
"""

import logging
import datetime
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import telemedicine as tm
from .models import Booking, Doctor, OnlineClinicSession

DESK = "telemedicine"

logger = logging.getLogger(__name__)


def can_host(user):
    from .views import is_admin_user, user_has_desk
    return bool(user and user.is_authenticated and (is_admin_user(user) or user_has_desk(user, DESK)))


def _staff_name(request):
    u = request.user
    return (u.get_full_name() or u.first_name or u.username) if u and u.is_authenticated else ""


def _err(msg, code=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({"error": msg, **extra}, status=code)


def _broadcast(booking, message):
    try:
        from .views import broadcast_booking_update
        broadcast_booking_update(booking, event_type="BOOKING_UPDATE", message=message)
    except Exception:
        pass


class _HostView(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not can_host(request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("The Online Clinic desk is needed for this.")

    def get_session(self, session_id, lock=False):
        qs = OnlineClinicSession.objects.select_related("doctor", "doctor__department")
        if lock:
            qs = qs.select_for_update()
        return qs.filter(session_id=session_id).first()


def _validate_times(date_s, start_s, end_s, slot_minutes):
    try:
        day = datetime.date.fromisoformat(str(date_s or "")[:10])
    except ValueError:
        return None, "Choose the date of the online clinic."
    start, end = tm.parse_hhmm(start_s), tm.parse_hhmm(end_s)
    if start is None or end is None:
        return None, "Enter start and end times like 09:00 and 12:00."
    try:
        slot = int(slot_minutes or 15)
    except (TypeError, ValueError):
        slot = 0
    if slot not in tm.SLOT_CHOICES:
        return None, f"Slot length must be one of {', '.join(str(x) for x in tm.SLOT_CHOICES)} minutes."
    if datetime.datetime.combine(day, end) - datetime.datetime.combine(day, start) < datetime.timedelta(minutes=slot):
        return None, "The end time must be at least one slot after the start time."
    if day < timezone.localdate():
        return None, "The online clinic can't be in the past."
    return (day.isoformat(), start.strftime("%H:%M"), end.strftime("%H:%M"), slot), ""


def _overlaps(doctor, day, start, end, exclude_id=None):
    qs = OnlineClinicSession.objects.filter(doctor=doctor, date=day).exclude(status=OnlineClinicSession.STATUS_CANCELLED)
    if exclude_id:
        qs = qs.exclude(session_id=exclude_id)
    return any(not (end <= s.start_time or start >= s.end_time) for s in qs)


class OnlineSessionListView(_HostView):
    def get(self, request):
        q = request.query_params
        qs = OnlineClinicSession.objects.select_related("doctor", "doctor__department")
        today = timezone.localdate()
        qs = qs.filter(date__gte=q.get("date_from") or (today - datetime.timedelta(days=7)).isoformat())
        if q.get("date_to"):
            qs = qs.filter(date__lte=q["date_to"])
        if q.get("doctor_id"):
            qs = qs.filter(doctor__doc_id=q["doctor_id"])
        return Response([tm.session_payload(s) for s in qs.order_by("date", "start_time")[:300]])

    def post(self, request):
        d = request.data
        doctor = Doctor.objects.select_related("department").filter(doc_id=str(d.get("doctor_id") or d.get("doctorId") or "")).first()
        if doctor is None:
            return _err("Choose the doctor for this online clinic.")
        if tm.online_fee(doctor) <= 0:
            return _err("Set an online consultation fee for this clinic first (Clinics, edit the clinic).")
        times, problem = _validate_times(d.get("date"), d.get("start_time") or d.get("startTime"),
                                         d.get("end_time") or d.get("endTime"), d.get("slot_minutes") or d.get("slotMinutes"))
        if problem:
            return _err(problem)
        day, start, end, slot = times
        if _overlaps(doctor, day, start, end):
            return _err("This doctor already has an online clinic at that time on that day.")
        session = OnlineClinicSession.objects.create(
            session_id=f"tm-{uuid.uuid4().hex[:12]}", doctor=doctor, date=day, start_time=start, end_time=end,
            slot_minutes=slot, notes=str(d.get("notes") or "")[:300], created_by=_staff_name(request),
        )
        return Response(tm.session_payload(session), status=status.HTTP_201_CREATED)


class OnlineSessionDetailView(_HostView):
    def get(self, request, session_id):
        s = self.get_session(session_id)
        if s is None:
            return _err("Online clinic not found.", status.HTTP_404_NOT_FOUND)
        return Response(tm.session_payload(s, with_queue=True))

    def patch(self, request, session_id):
        d = request.data
        with transaction.atomic():
            s = self.get_session(session_id, lock=True)
            if s is None:
                return _err("Online clinic not found.", status.HTTP_404_NOT_FOUND)
            if s.status in (OnlineClinicSession.STATUS_ENDED, OnlineClinicSession.STATUS_CANCELLED):
                return _err("This online clinic has ended or was cancelled.")
            if "notes" in d:
                s.notes = str(d.get("notes") or "")[:300]
            wants_times = any(k in d for k in ("date", "start_time", "startTime", "end_time", "endTime", "slot_minutes", "slotMinutes"))
            if wants_times:
                booked = tm.active_online_qs().filter(online_session=s).exists()
                times, problem = _validate_times(d.get("date", s.date), d.get("start_time") or d.get("startTime") or s.start_time,
                                                 d.get("end_time") or d.get("endTime") or s.end_time,
                                                 d.get("slot_minutes") or d.get("slotMinutes") or s.slot_minutes)
                if problem:
                    return _err(problem)
                day, start, end, slot = times
                if booked and (day != s.date or start != s.start_time or slot != s.slot_minutes or end < s.end_time):
                    return _err("Patients have booked this online clinic: only a later end time can be set now.")
                if _overlaps(s.doctor, day, start, end, exclude_id=s.session_id):
                    return _err("This doctor already has an online clinic at that time on that day.")
                s.date, s.start_time, s.end_time, s.slot_minutes = day, start, end, slot
            s.save()
        return Response(tm.session_payload(s))


class OnlineSessionActionView(_HostView):
    """start / end / cancel / admit / extend / end-call / missed."""

    def post(self, request, session_id, action):
        handler = {
            "start": self._start, "end": self._end, "cancel": self._cancel, "admit": self._admit,
            "extend": self._extend, "end-call": self._end_call, "missed": self._missed,
        }.get(action)
        if handler is None:
            return _err("Unknown action.", status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            s = self.get_session(session_id, lock=True)
            if s is None:
                return _err("Online clinic not found.", status.HTTP_404_NOT_FOUND)
            result = handler(request, s)
        if isinstance(result, Response):
            return result
        self._alert_after(action, request, s)
        return Response(tm.session_payload(s, with_queue=True))

    def _alert_after(self, action, request, s):
        """SMS nudges for patients who aren't on their consultation page
        (sent once each, in the background; never blocks the doctor)."""
        try:
            if action == "start":
                tm.alert_soon_patients(s)
            elif action == "admit":
                ref = str(request.data.get("ref_code") or request.data.get("refCode") or "").strip()
                b = tm.active_online_qs().filter(online_session=s, ref_code=ref).first()
                if b is not None:
                    tm.alert_patient(b, "doctor_ready")
            elif action in ("end-call", "missed"):
                tm.alert_next_patient(s)
        except Exception:  # pragma: no cover
            logger.exception("Online clinic alert failed")

    # ---- session ------------------------------------------------------
    def _start(self, request, s):
        if s.status == OnlineClinicSession.STATUS_LIVE:
            return None
        if s.status != OnlineClinicSession.STATUS_SCHEDULED:
            return _err("This online clinic has ended or was cancelled.")
        start, end = tm.session_bounds(s)
        now = timezone.now()
        if now < start - datetime.timedelta(minutes=60) or now > end + datetime.timedelta(minutes=tm.MAX_EXTENSION_MINUTES):
            return _err("An online clinic can be started from an hour before its start time on the day.")
        s.status = OnlineClinicSession.STATUS_LIVE
        s.started_at = now
        s.save(update_fields=["status", "started_at", "updated_at"])
        for b in tm.active_online_qs().filter(online_session=s):
            tm.notify_patient(b, "doctor_online")
        return None

    def _end(self, request, s):
        if s.status not in (OnlineClinicSession.STATUS_LIVE, OnlineClinicSession.STATUS_SCHEDULED):
            return None
        now = timezone.now()
        for b in tm.active_online_qs().filter(online_session=s, call_status=tm.CALL_IN):
            self._finish(b, now)
        s.status = OnlineClinicSession.STATUS_ENDED
        s.ended_at = now
        s.save(update_fields=["status", "ended_at", "updated_at"])
        for b in tm.active_online_qs().filter(online_session=s):
            tm.notify_patient(b, "session_ended")
        return None

    def _cancel(self, request, s):
        if tm.active_online_qs().filter(online_session=s).exists():
            return _err("Patients have booked this online clinic, so it can't be cancelled here. "
                        "Contact them (and refund any payment) first.")
        s.status = OnlineClinicSession.STATUS_CANCELLED
        s.save(update_fields=["status", "updated_at"])
        return None

    # ---- patients -----------------------------------------------------
    def _booking(self, request, s):
        ref = str(request.data.get("ref_code") or request.data.get("refCode") or "").strip()
        b = tm.active_online_qs().select_for_update().filter(online_session=s, ref_code=ref).first()
        return b

    def _finish(self, b, now, status_value=tm.CALL_DONE):
        b.call_status = status_value
        b.call_ended_at = now
        if status_value == tm.CALL_DONE:
            b.status = "Completed"
        b.save(update_fields=["call_status", "call_ended_at", "status"])
        tm.notify_patient(b, "call_ended")

    def _admit(self, request, s):
        if s.status != OnlineClinicSession.STATUS_LIVE:
            return _err("Start the online clinic first.")
        b = self._booking(request, s)
        if b is None:
            return _err("That patient isn't booked in this online clinic.", status.HTTP_404_NOT_FOUND)
        if not tm.is_paid(b):
            return _err("This patient hasn't paid for the online consultation.")
        if b.call_status in (tm.CALL_DONE, tm.CALL_MISSED):
            return _err("This patient's consultation has already ended.")
        now = timezone.now()
        busy = tm.active_online_qs().filter(online_session=s, call_status=tm.CALL_IN).exclude(ref_code=b.ref_code).first()
        if busy is not None:
            return _err(f"Finish the call with {busy.patient_name} first.")
        fields = ["call_status", "call_started_at"]
        if not b.call_started_at:
            b.call_started_at = now
            # The doctor is running late (an earlier call ran over): the patient
            # still gets their full slot length, counted from now.
            if b.slot_start and b.slot_end and now > b.slot_start:
                late_end = now + (b.slot_end - b.slot_start)
                if late_end > b.slot_end:
                    b.slot_end = late_end
                    fields.append("slot_end")
        b.call_status = tm.CALL_IN
        b.save(update_fields=fields)
        tm.notify_patient(b, "admitted")
        _broadcast(b, f"Online consultation started: {b.patient_name}.")
        return None

    def _extend(self, request, s):
        b = self._booking(request, s)
        if b is None:
            return _err("That patient isn't booked in this online clinic.", status.HTTP_404_NOT_FOUND)
        try:
            minutes = int(request.data.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes not in tm.EXTEND_CHOICES:
            return _err(f"Extend by {', '.join(str(x) for x in tm.EXTEND_CHOICES)} minutes.")
        if b.call_status in (tm.CALL_DONE, tm.CALL_MISSED):
            return _err("This patient's consultation has already ended.")
        if int(b.call_extended_minutes or 0) + minutes > tm.MAX_EXTENSION_MINUTES:
            return _err(f"A consultation can be extended by up to {tm.MAX_EXTENSION_MINUTES} minutes in all.")
        now = timezone.now()
        # Extending after the time ran out continues from now, not from the old end.
        if b.effective_slot_end and now > b.effective_slot_end:
            gap = int((now - b.effective_slot_end).total_seconds() // 60) + 1
            if int(b.call_extended_minutes or 0) + gap + minutes > tm.MAX_EXTENSION_MINUTES:
                return _err(f"A consultation can be extended by up to {tm.MAX_EXTENSION_MINUTES} minutes in all.")
            minutes += gap
        b.call_extended_minutes = int(b.call_extended_minutes or 0) + minutes
        b.save(update_fields=["call_extended_minutes"])
        tm.notify_patient(b, "extended")
        return None

    def _end_call(self, request, s):
        b = self._booking(request, s)
        if b is None:
            return _err("That patient isn't booked in this online clinic.", status.HTTP_404_NOT_FOUND)
        if b.call_status != tm.CALL_IN:
            return _err("There is no call in progress with this patient.")
        self._finish(b, timezone.now())
        _broadcast(b, f"Online consultation completed: {b.patient_name}.")
        return None

    def _missed(self, request, s):
        b = self._booking(request, s)
        if b is None:
            return _err("That patient isn't booked in this online clinic.", status.HTTP_404_NOT_FOUND)
        if b.call_status == tm.CALL_IN:
            return _err("End the call first.")
        if b.call_status == tm.CALL_DONE:
            return _err("This consultation is already completed.")
        self._finish(b, timezone.now(), tm.CALL_MISSED)
        return None


class PublicOnlineSlotsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        q = request.query_params
        doctor = None
        if q.get("doctor_id"):
            doctor = Doctor.objects.select_related("department").filter(doc_id=q["doctor_id"]).first()
            if doctor is None:
                return Response({"sessions": [], "fee": 0})
        sessions = tm.bookable_sessions(doctor=doctor, department_id=q.get("department") or None)
        fee = float(tm.online_fee(doctor)) if doctor is not None else (sessions[0]["fee"] if sessions else 0)
        return Response({"sessions": sessions, "fee": fee, "server_time": timezone.localtime().isoformat()})


class PatientJoinView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        b = Booking.objects.select_related("online_session").filter(video_token=token, consultation_mode=tm.ONLINE).first() if token else None
        if b is None or len(token) < 20:
            return _err("This consultation link isn't valid. Check the link on your ticket.", status.HTTP_404_NOT_FOUND)
        return Response(tm.join_state(b))
