"""
Online (video) consultations: telemedicine.

How it fits the existing system
-------------------------------
- A doctor's online clinic is an OnlineClinicSession: one date, start and
  end time, split into slots (default 15 minutes). Sessions are separate
  from the in-person roster, so in-person days, capacity, check-in and the
  Monitor are not affected.
- A patient books a slot through the normal booking + Paystack flow with
  consultation_mode="online"; the fee is the clinic's online_fee. The
  booking keeps the slot (its timeframe) and a secret video_token for the
  join link /consult/<video_token>.
- On the day the doctor starts the session, sees the queue (in slot order,
  with who is waiting in the room), admits one patient at a time, can
  extend that patient's time, and ends the call. The video itself is a
  direct WebRTC connection between the two browsers; this server only
  relays the connection set-up messages (signalling) over a WebSocket, and
  only while the doctor has admitted that patient and their time hasn't run
  out.

Settings (.env)
---------------
TELEMEDICINE_ICE_SERVERS   JSON list of ICE servers. Default: Google's
                           public STUN. For patients on mobile data or strict
                           networks add a TURN server (e.g. coturn):
                           [{"urls":"stun:stun.l.google.com:19302"},
                            {"urls":"turn:turn.example.com:3478","username":"u","credential":"p"}]
TELEMEDICINE_JOIN_EARLY_MINUTES  How early the patient may enter the waiting
                           room before their slot (default 15).
TELEMEDICINE_TURN_URLS     Comma-separated TURN URLs, e.g.
                           turn:turn.example.com:3478,turns:turn.example.com:5349
TELEMEDICINE_TURN_SECRET   coturn static-auth-secret: credentials are minted
                           per call and expire (TELEMEDICINE_TURN_TTL_SECONDS).
"""

import threading
import datetime
import json
import logging
import secrets

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

ONLINE = "online"
IN_PERSON = "in_person"
CALL_IN = "in_call"
CALL_DONE = "completed"
CALL_MISSED = "missed"
EXTEND_CHOICES = (5, 10, 15)
MAX_EXTENSION_MINUTES = 60
SIGNAL_GRACE_SECONDS = 60
PRESENCE_TTL = 45
SLOT_CHOICES = (10, 15, 20, 30, 45, 60)


class TelemedicineError(Exception):
    pass


def join_early_minutes():
    return int(getattr(settings, "TELEMEDICINE_JOIN_EARLY_MINUTES", 15) or 15)


def _base_ice_servers():
    raw = getattr(settings, "TELEMEDICINE_ICE_SERVERS", "") or ""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw) if raw else None
        if isinstance(value, list) and value:
            return value
    except (TypeError, ValueError):
        logger.warning("TELEMEDICINE_ICE_SERVERS is not valid JSON; using public STUN.")
    return [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]


def turn_rest_credentials(secret, ttl_seconds=None, user="isalu", now=None):
    """Time-limited TURN username/password (coturn `use-auth-secret` /
    `static-auth-secret`): username = "<expiry>:<user>", password =
    base64(HMAC-SHA1(secret, username))."""
    import base64
    import hashlib
    import hmac
    ttl = int(ttl_seconds or getattr(settings, "TELEMEDICINE_TURN_TTL_SECONDS", 6 * 3600) or 6 * 3600)
    expiry = int((now or timezone.now()).timestamp()) + ttl
    username = f"{expiry}:{user}"
    password = base64.b64encode(hmac.new(str(secret).encode(), username.encode(), hashlib.sha1).digest()).decode()
    return username, password


def ice_servers():
    """STUN/TURN servers for the browsers. With TELEMEDICINE_TURN_URLS and
    TELEMEDICINE_TURN_SECRET set, a TURN server with fresh time-limited
    credentials is added (relays calls when a direct connection is blocked,
    common on mobile data and office networks)."""
    servers = _base_ice_servers()
    urls = str(getattr(settings, "TELEMEDICINE_TURN_URLS", "") or "").strip()
    secret = str(getattr(settings, "TELEMEDICINE_TURN_SECRET", "") or "").strip()
    if urls and secret:
        username, credential = turn_rest_credentials(secret)
        servers = servers + [{"urls": [u.strip() for u in urls.split(",") if u.strip()],
                              "username": username, "credential": credential}]
    return servers


def online_fee(doctor):
    """The clinic's online consultation fee (Decimal); 0 = no online consultations."""
    from decimal import Decimal
    if doctor is None or not doctor.department_id:
        return Decimal("0")
    return Decimal(doctor.department.online_fee or 0).quantize(Decimal("0.01"))


def new_video_token():
    return secrets.token_urlsafe(24)


def join_url(booking):
    """Absolute join link when PUBLIC_SITE_URL is set, else the site-relative path."""
    if not booking.video_token:
        return ""
    base = str(getattr(settings, "PUBLIC_SITE_URL", "") or "").rstrip("/")
    return f"{base}/consult/{booking.video_token}"


# ---------------------------------------------------------------------------
# Times and slots
# ---------------------------------------------------------------------------

def parse_hhmm(value):
    try:
        h, m = str(value or "").strip().split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return datetime.time(h, m)
    except (TypeError, ValueError):
        pass
    return None


def local_dt(date_str, hhmm):
    day = datetime.date.fromisoformat(str(date_str)[:10])
    t = parse_hhmm(hhmm) if not isinstance(hhmm, datetime.time) else hhmm
    return timezone.make_aware(datetime.datetime.combine(day, t), timezone.get_current_timezone())


def fmt_time(dt):
    return timezone.localtime(dt).strftime("%I:%M %p").lstrip("0")


def slot_label(start, end):
    return f"{fmt_time(start)} – {fmt_time(end)}"


def session_bounds(session):
    return local_dt(session.date, session.start_time), local_dt(session.date, session.end_time)


def session_slots(session):
    start, end = session_bounds(session)
    step = datetime.timedelta(minutes=int(session.slot_minutes or 15))
    out = []
    cur = start
    while cur + step <= end:
        out.append((cur, cur + step))
        cur += step
    return out


def active_online_qs(queryset=None):
    """Online bookings that hold their slot (paid, or an unexpired payment hold)."""
    from .scheduling import active_bookings_qs
    return active_bookings_qs(queryset).filter(consultation_mode=ONLINE)


def taken_slot_starts(session, exclude_ref=None):
    qs = active_online_qs().filter(online_session=session)
    if exclude_ref:
        qs = qs.exclude(ref_code=exclude_ref)
    return {timezone.localtime(s).replace(second=0, microsecond=0) for s in qs.values_list("slot_start", flat=True) if s}


def slot_bookable_from(now=None):
    """Slots must start at least this far ahead to be booked (time to pay and join)."""
    return (now or timezone.now()) + datetime.timedelta(minutes=5)


def available_slots(session, now=None):
    now = now or timezone.now()
    taken = taken_slot_starts(session)
    cutoff = slot_bookable_from(now)
    out = []
    for s, e in session_slots(session):
        out.append({
            "start": timezone.localtime(s).isoformat(),
            "end": timezone.localtime(e).isoformat(),
            "label": slot_label(s, e),
            "time": fmt_time(s),
            "available": s >= cutoff and timezone.localtime(s).replace(second=0, microsecond=0) not in taken,
        })
    return out


def find_slot(session, raw):
    """The (start, end) slot of `session` matching raw ('HH:MM' or ISO datetime), or None."""
    want = None
    text = str(raw or "").strip()
    t = parse_hhmm(text) if len(text) <= 5 else None
    if t is not None:
        want = local_dt(session.date, t)
    elif text:
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(text)
        if dt is not None:
            want = dt if timezone.is_aware(dt) else timezone.make_aware(dt, timezone.get_current_timezone())
    if want is None:
        return None
    for s, e in session_slots(session):
        if abs((s - want).total_seconds()) < 30:
            return s, e
    return None


def bookable_sessions(doctor=None, department_id=None, days=30, now=None):
    """Upcoming scheduled/live online sessions that still have a free slot."""
    from .models import OnlineClinicSession
    now = now or timezone.now()
    today = timezone.localdate()
    qs = OnlineClinicSession.objects.select_related("doctor", "doctor__department").filter(
        status__in=(OnlineClinicSession.STATUS_SCHEDULED, OnlineClinicSession.STATUS_LIVE),
        date__gte=today.isoformat(),
        date__lte=(today + datetime.timedelta(days=days)).isoformat(),
        doctor__status=True,
    )
    if doctor is not None:
        qs = qs.filter(doctor=doctor)
    if department_id:
        qs = qs.filter(doctor__department_id=department_id)
    out = []
    for s in qs:
        if online_fee(s.doctor) <= 0:
            continue
        slots = available_slots(s, now)
        free = [x for x in slots if x["available"]]
        if not free:
            continue
        out.append({
            "session_id": s.session_id,
            "doctor_id": s.doctor.doc_id,
            "doctor_acronym": (s.doctor.acronym or s.doctor.name or "").strip(),
            "clinic": s.doctor.department.name if s.doctor.department else "",
            "date": s.date,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "slot_minutes": s.slot_minutes,
            "status": s.status,
            "fee": float(online_fee(s.doctor)),
            "free_slots": len(free),
            "slots": slots,
        })
    return out


# ---------------------------------------------------------------------------
# Patient side
# ---------------------------------------------------------------------------

def is_paid(booking):
    return str(booking.payment_status or "").strip().lower() in ("cleared", "paid", "settled")


def queue_position(booking):
    """The patient's place in line: 1 + paid patients with an earlier slot not yet seen."""
    if not booking.online_session_id or not booking.slot_start:
        return 0
    ahead = active_online_qs().filter(
        online_session_id=booking.online_session_id,
        slot_start__lt=booking.slot_start,
        payment_status__in=("Cleared", "cleared", "Paid", "paid", "Settled", "settled"),
    ).exclude(call_status__in=(CALL_DONE, CALL_MISSED)).count()
    return ahead + 1   # 1 = you're next


def join_state(booking, now=None):
    """What the patient's consultation page should show right now."""
    from .models import OnlineClinicSession
    now = now or timezone.now()
    session = booking.online_session
    start = booking.slot_start
    end = booking.effective_slot_end
    opens = start - datetime.timedelta(minutes=join_early_minutes()) if start else None
    st = str(booking.status or "").strip().lower()
    if not booking.is_active or st in ("cancelled", "canceled", "disabled", "payment expired"):
        state = "cancelled"
    elif not is_paid(booking):
        state = "not_paid"
    elif booking.call_status == CALL_DONE:
        state = "completed"
    elif booking.call_status == CALL_MISSED:
        state = "missed"
    elif session is None or session.status == OnlineClinicSession.STATUS_CANCELLED:
        state = "cancelled"
    elif booking.call_status == CALL_IN:
        state = "in_call" if end and now <= end + datetime.timedelta(seconds=SIGNAL_GRACE_SECONDS) else "completed"
    elif session.status == OnlineClinicSession.STATUS_ENDED:
        state = "ended"
    elif session.status == OnlineClinicSession.STATUS_LIVE:
        # The clinic is running: keep waiting even if the doctor is running late.
        state = "waiting"
    elif end and now > max(end, session_bounds(session)[1]) + datetime.timedelta(hours=1):
        state = "ended"     # the doctor never started this online clinic
    elif opens and now < opens:
        state = "too_early"
    else:
        state = "waiting"
    return {
        "state": state,
        "ref_code": booking.ref_code,
        "patient_name": booking.patient_name,
        "clinic": booking.doctor_specialty,
        "doctor_acronym": _doctor_acronym(booking.doctor_id),
        "date": booking.date,
        "slot_label": slot_label(start, booking.slot_end) if start and booking.slot_end else booking.time,
        "slot_start": timezone.localtime(start).isoformat() if start else None,
        "slot_end": timezone.localtime(end).isoformat() if end else None,
        "extended_minutes": int(booking.call_extended_minutes or 0),
        "opens_at": timezone.localtime(opens).isoformat() if opens else None,
        "session_status": session.status if session else "",
        "doctor_online": bool(session and session.status == OnlineClinicSession.STATUS_LIVE),
        "position": queue_position(booking) if state == "waiting" else 0,
        "server_time": timezone.localtime(now).isoformat(),
        "ice_servers": ice_servers() if state in ("waiting", "in_call", "too_early") else [],
    }


def _doctor_acronym(doc_id):
    from .models import Doctor
    d = Doctor.objects.filter(doc_id=doc_id).only("acronym", "name").first()
    return (d.acronym or d.name or "").strip() if d else ""


def signalling_allowed(booking, now=None):
    now = now or timezone.now()
    end = booking.effective_slot_end
    return (booking.call_status == CALL_IN and end is not None
            and now <= end + datetime.timedelta(seconds=SIGNAL_GRACE_SECONDS))


# ---------------------------------------------------------------------------
# Presence (patient has the page open) and live events
# ---------------------------------------------------------------------------

def _presence_key(ref):
    return f"tm_presence:{ref}"


def set_presence(ref, online=True):
    if online:
        cache.set(_presence_key(ref), timezone.now().isoformat(), PRESENCE_TTL)
    else:
        cache.delete(_presence_key(ref))


def is_present(ref):
    return bool(cache.get(_presence_key(ref)))


def patient_group(ref):
    return f"tm_p_{ref}".replace("-", "_")


def doctor_group(session_id):
    return f"tm_d_{session_id}".replace("-", "_")


def send_event(group, payload):
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(group, {"type": "tm.event", "payload": payload})
    except Exception:
        logger.exception("telemedicine event to %s failed", group)


def notify_patient(booking, kind, **extra):
    send_event(patient_group(booking.ref_code), {"event": kind, "state": join_state(booking), **extra})


def notify_doctor(session_id, kind, **extra):
    send_event(doctor_group(session_id), {"event": kind, **extra})


# ---------------------------------------------------------------------------
# Doctor side
# ---------------------------------------------------------------------------

def queue_payload(session, now=None):
    now = now or timezone.now()
    rows = []
    for b in active_online_qs().filter(online_session=session).order_by("slot_start"):
        end = b.effective_slot_end
        rows.append({
            "ref_code": b.ref_code,
            "patient_name": b.patient_name,
            "patient_phone": b.patient_phone,
            "visit_type": b.visit_type or "First Visit",
            "slot_label": slot_label(b.slot_start, b.slot_end) if b.slot_start and b.slot_end else b.time,
            "slot_start": timezone.localtime(b.slot_start).isoformat() if b.slot_start else None,
            "slot_end": timezone.localtime(end).isoformat() if end else None,
            "extended_minutes": int(b.call_extended_minutes or 0),
            "paid": is_paid(b),
            "payment_status": b.payment_status,
            "call_status": b.call_status or "",
            "in_waiting_room": is_present(b.ref_code),
            "time_up": bool(end and now > end),
        })
    return rows


def session_payload(session, with_queue=False):
    start, end = session_bounds(session)
    slots = session_slots(session)
    booked = active_online_qs().filter(online_session=session).count()
    data = {
        "session_id": session.session_id,
        "doctor_id": session.doctor_id,
        "doctor_name": session.doctor.full_name or session.doctor.name,
        "doctor_acronym": (session.doctor.acronym or "").strip(),
        "clinic": session.doctor.department.name if session.doctor.department else "",
        "online_fee": float(online_fee(session.doctor)),
        "date": session.date,
        "start_time": session.start_time,
        "end_time": session.end_time,
        "slot_minutes": session.slot_minutes,
        "slots_total": len(slots),
        "slots_booked": booked,
        "status": session.status,
        "notes": session.notes,
        "starts_at": timezone.localtime(start).isoformat(),
        "ends_at": timezone.localtime(end).isoformat(),
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    }
    if with_queue:
        data["queue"] = queue_payload(session)
        data["ice_servers"] = ice_servers()
    return data


# ---------------------------------------------------------------------------
# SMS alerts for patients who aren't on their consultation page
# ---------------------------------------------------------------------------

ALERT_TTL = 12 * 3600


def alert_patient(booking, kind):
    """Send an online-consultation SMS (doctor_started / up_next / doctor_ready)
    once per booking and kind, in the background, only if the patient isn't
    already on their consultation page."""
    if not booking or not booking.video_token or is_present(booking.ref_code):
        return False
    key = f"tm_alert:{kind}:{booking.ref_code}"
    if not cache.add(key, 1, ALERT_TTL):
        return False          # already sent

    def _run(ref):
        try:
            from django.db import close_old_connections
            from .models import Booking
            from .notification_service import send_online_notice
            b = Booking.objects.filter(ref_code=ref).first()
            if b is not None:
                send_online_notice(b, kind)
        except Exception:  # pragma: no cover
            logger.exception("Online consultation alert failed for %s", ref)
        finally:
            try:
                close_old_connections()
            except Exception:
                pass

    if getattr(settings, "TELEMEDICINE_ALERTS_SYNC", False):
        _run(booking.ref_code)
    else:
        threading.Thread(target=_run, args=(booking.ref_code,), daemon=True).start()
    return True


def waiting_unseen(session):
    """Paid patients in this session not yet seen, in slot order."""
    return [b for b in active_online_qs().filter(online_session=session).exclude(call_status__in=(CALL_IN, CALL_DONE, CALL_MISSED)).order_by("slot_start")
            if is_paid(b)]


def alert_soon_patients(session, within_minutes=30, now=None):
    """Clinic started: tell paid patients due within `within_minutes` who aren't on their page."""
    now = now or timezone.now()
    sent = 0
    for b in waiting_unseen(session):
        if b.slot_start and b.slot_start <= now + datetime.timedelta(minutes=within_minutes):
            sent += bool(alert_patient(b, "doctor_started"))
    return sent


def alert_next_patient(session):
    """After a call: nudge the next patient in line if they aren't on their page."""
    nxt = waiting_unseen(session)
    return alert_patient(nxt[0], "up_next") if nxt else False
