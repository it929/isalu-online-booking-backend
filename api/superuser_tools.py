"""
Superuser bookings tools (hidden page).

Bulk-upload bookings from a CSV file, download all bookings or upcoming
bookings as CSV, and remove all bookings.

Access, in layers:
1. The page exists only when SUPERUSER_TOOLS_PATH is set in the environment
   (a long secret slug). It is served at /api/<slug>/; any other path is a
   plain 404, and nothing in the website or dashboard links to it.
2. Only a Django superuser can use it. Anyone else signed in gets a 404;
   someone not signed in sees a sign-in form (superusers only).
3. Every action also needs the tools password, set with
   `python manage.py set_tools_password` (stored hashed, never in .env).
4. Uploads are CSV only and go through a preview first; nothing is written
   until the superuser confirms. Removing all bookings needs the password, a
   typed confirmation phrase and a confirm dialog, and a full backup CSV is
   written to the server first.

Two-step verification (Google Authenticator or any TOTP app) is required:
5. Sign-in needs the superuser's password AND a 6-digit authenticator code.
   Being signed in to /admin/ is not enough: the page keeps its own
   verification, which ends after 15 minutes without activity.
6. Every action also needs a fresh authenticator code. Each code works once.
   The authenticator is set up on the server with
   `python manage.py setup_tools_2fa <username>`.
7. Every sign-in, lock-out, upload, download and removal is emailed to the
   superusers (and SUPERUSER_TOOLS_ALERT_EMAIL), when email is configured.

Failed sign-ins, wrong tools passwords and wrong codes lock the page for
15 minutes after 5 attempts. Every action is logged (who, when, counts).
"""

import csv
import datetime
import io
import logging
import os
import re
import secrets
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from . import totp
from .models import AppSetting, Booking

logger = logging.getLogger("superuser_tools")

PASSWORD_KEY = "superuser_tools_password"
TWOFA_KEY = "superuser_tools_2fa:{}"
SESSION_FLAG = "su_tools_verified"
ENROL_FLAG = "su_tools_enrol"
ENROL_SECONDS = 10 * 60
IDLE_SECONDS = 15 * 60
LOG_KEY = "superuser_tools_log"
CONFIRM_PHRASE = "DELETE ALL BOOKINGS"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10000
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
STAGE_SECONDS = 15 * 60

# Columns written and read: every booking field except the uploaded referral
# document itself (base64 file data; the backup before a delete keeps it).
EXPORT_EXCLUDE = {"referral_doc_data"}
REQUIRED = ("patient_name", "patient_phone", "date", "time")
NOT_UPCOMING = ("cancelled", "canceled", "disabled", "completed", "awaiting payment", "payment expired")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tools_dir():
    path = getattr(settings, "SUPERUSER_TOOLS_DIR", None) or os.path.join(settings.BASE_DIR, "superuser_tools_data")
    os.makedirs(os.path.join(path, "staged"), exist_ok=True)
    os.makedirs(os.path.join(path, "backups"), exist_ok=True)
    return path


def _ip(request):
    return (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
            or request.META.get("REMOTE_ADDR", "") or "unknown")


def _lock_key(request):
    return f"su_tools_fail:{_ip(request)}"


def _locked(request):
    return (cache.get(_lock_key(request)) or 0) >= MAX_FAILURES


def _fail(request):
    key = _lock_key(request)
    cache.set(key, (cache.get(key) or 0) + 1, LOCK_SECONDS)


def _clear_fail(request):
    cache.delete(_lock_key(request))


def twofa_record(user):
    s = AppSetting.objects.filter(key=TWOFA_KEY.format(user.pk)).first()
    v = s.value if s and isinstance(s.value, dict) else {}
    return v if v.get("secret") and v.get("confirmed_at") else None


def _code_ok(request, user, code):
    """Check an authenticator code; each code is accepted once. Counts towards the lock-out."""
    with transaction.atomic():
        s = AppSetting.objects.select_for_update().filter(key=TWOFA_KEY.format(user.pk)).first()
        v = dict(s.value) if s and isinstance(s.value, dict) else {}
        counter = totp.verify(v.get("secret", ""), code, int(v.get("last_counter", -1))) if v.get("confirmed_at") else None
        if counter is None:
            _fail(request)
            return False
        v["last_counter"] = counter
        s.value = v
        s.save(update_fields=["value", "updated_at"])
    _clear_fail(request)
    return True


def _alert(request, subject, body):
    """Email the superusers (best effort, in the background)."""
    from django.contrib.auth.models import User
    to = set(User.objects.filter(is_superuser=True, is_active=True).exclude(email="").values_list("email", flat=True))
    extra = str(getattr(settings, "SUPERUSER_TOOLS_ALERT_EMAIL", "") or "")
    to |= {e.strip() for e in extra.split(",") if "@" in e}
    if not to:
        return
    who = request.user.get_username() if request.user.is_authenticated else (request.POST.get("username", "") or "unknown")
    text = (f"{body}\n\nAccount: {who}\nFrom IP: {_ip(request)}\n"
            f"Time: {timezone.localtime().strftime('%d %b %Y, %H:%M:%S')}\n\n"
            "If this wasn't you, change the superuser password, reset the tools password and authenticator, "
            "and clear SUPERUSER_TOOLS_PATH to switch the page off.")
    sender = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None)

    def send():
        try:
            from django.core.mail import send_mail
            send_mail(f"[Isalu bookings tools] {subject}", text, sender, sorted(to), fail_silently=False)
        except Exception:
            logger.exception("superuser tools alert email failed")

    import threading
    threading.Thread(target=send, daemon=True).start()


def qr_svg(text):
    """The text as an inline SVG QR code, or "" when the qrcode package isn't installed."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    svg = img.to_string(encoding="unicode")
    return re.sub(r'width="[^"]*" height="[^"]*"', 'width="220" height="220"', svg, count=1)


def _enrol_page(request, user, secret, error=""):
    uri = totp.provisioning_uri(secret, user.get_username())
    response = render(request, "superuser_tools/enrol.html", {
        "error": error, "qr": qr_svg(uri), "uri": uri,
        "key": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "username": user.get_username(), "locked": _locked(request),
    })
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _verified(request):
    """This browser passed the tools sign-in (password + code) and has been active in the last 15 minutes."""
    seen = request.session.get(SESSION_FLAG)
    if not seen or seen.get("user") != request.user.pk:
        return False
    if timezone.now().timestamp() - float(seen.get("at", 0)) > IDLE_SECONDS:
        request.session.pop(SESSION_FLAG, None)
        return False
    request.session[SESSION_FLAG] = {"user": request.user.pk, "at": timezone.now().timestamp()}
    return True


def password_is_set():
    s = AppSetting.objects.filter(key=PASSWORD_KEY).first()
    return bool(s and isinstance(s.value, dict) and s.value.get("hash"))


def _password_ok(request, raw):
    s = AppSetting.objects.filter(key=PASSWORD_KEY).first()
    stored = (s.value or {}).get("hash") if s and isinstance(s.value, dict) else ""
    ok = bool(stored and raw and check_password(raw, stored))
    if not ok:
        _fail(request)
    # A right password doesn't reset the failure count: only password + a right
    # authenticator code does (in _code_ok), so codes can't be guessed endlessly.
    return ok


def _audit(request, action, detail=""):
    entry = {
        "at": timezone.now().isoformat(),
        "user": request.user.get_username() if request.user.is_authenticated else "",
        "ip": _ip(request),
        "action": action,
        "detail": str(detail)[:300],
    }
    logger.warning("superuser tools: %s by %s from %s: %s", action, entry["user"], entry["ip"], entry["detail"])
    with transaction.atomic():
        s, _ = AppSetting.objects.select_for_update().get_or_create(key=LOG_KEY, defaults={"value": {"entries": []}})
        entries = list((s.value or {}).get("entries", []))[-49:] + [entry]
        s.value = {"entries": entries}
        s.save(update_fields=["value", "updated_at"])


def _recent_log():
    s = AppSetting.objects.filter(key=LOG_KEY).first()
    entries = list((s.value or {}).get("entries", [])) if s else []
    out = []
    for e in reversed(entries[-15:]):
        at = parse_datetime(e.get("at", "") or "")
        out.append(dict(e, at_text=timezone.localtime(at).strftime("%d %b %Y, %H:%M") if at else e.get("at", "")))
    return out


def export_columns(include_all=False):
    return [f.name for f in Booking._meta.concrete_fields if include_all or f.name not in EXPORT_EXCLUDE]


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return timezone.localtime(value).isoformat() if timezone.is_aware(value) else value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    # Spreadsheet formula injection: a cell starting with = + - @ is shown as text.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text


def _csv_rows(queryset, columns):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    yield "﻿" + buf.getvalue()  # BOM so Excel opens it as UTF-8
    for b in queryset.iterator(chunk_size=500):
        buf.seek(0)
        buf.truncate(0)
        writer.writerow([_cell(getattr(b, c)) for c in columns])
        yield buf.getvalue()


def _download(queryset, filename):
    response = StreamingHttpResponse(_csv_rows(queryset, export_columns()), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response


def upcoming_queryset():
    today = timezone.localdate().isoformat()
    qs = Booking.objects.filter(is_active=True, date__gte=today)
    for st in NOT_UPCOMING:
        qs = qs.exclude(status__iexact=st)
    return qs.order_by("date", "time")


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _unescape(text):
    text = str(text if text is not None else "").strip()
    if len(text) > 1 and text[0] == "'" and text[1] in ("=", "+", "-", "@"):
        text = text[1:]
    return text


def _field_value(field, raw):
    """Convert one CSV cell for a model field. Raises ValueError with a readable message."""
    from django.db import models as m

    if raw == "":
        if isinstance(field, (m.DateTimeField, m.DecimalField)) or field.null:
            return None
        if isinstance(field, m.BooleanField):
            return field.default if field.has_default() and not callable(field.default) else False
        return field.default if field.has_default() and not callable(field.default) else ""
    if isinstance(field, m.BooleanField):
        low = raw.lower()
        if low in ("true", "1", "yes", "y"):
            return True
        if low in ("false", "0", "no", "n"):
            return False
        raise ValueError(f"{field.name} must be true or false")
    if isinstance(field, m.DecimalField):
        try:
            return Decimal(raw.replace(",", "").replace("₦", ""))
        except InvalidOperation:
            raise ValueError(f"{field.name} must be a number")
    if isinstance(field, m.DateTimeField):
        dt = parse_datetime(raw.replace(" ", "T", 1) if "T" not in raw and " " in raw else raw)
        if dt is None:
            raise ValueError(f"{field.name} must be a date and time like 2026-10-31T09:00")
        return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
    if isinstance(field, m.EmailField) and raw and "@" not in raw:
        raise ValueError(f"{field.name} is not an email address")
    max_len = getattr(field, "max_length", None)
    if max_len and len(raw) > max_len:
        raise ValueError(f"{field.name} is longer than {max_len} characters")
    return raw


def parse_upload(data):
    """
    Returns {"rows": [dict, ...], "errors": [(line, message)], "skipped": [(line, ref, reason)],
             "columns": [...], "unknown": [...]}. Nothing is written.
    """
    text = data.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("This doesn't look like a text CSV file.")
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or []) if h is not None]
    if not header:
        raise ValueError("The CSV file is empty or has no header row.")
    reader.fieldnames = header
    fields = {f.name: f for f in Booking._meta.concrete_fields if f.name not in EXPORT_EXCLUDE}
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ValueError("Missing required column(s): " + ", ".join(missing) + ".")
    unknown = [h for h in header if h not in fields]

    existing = set(Booking.objects.values_list("ref_code", flat=True))
    seen = set()
    rows, errors, skipped = [], [], []
    for n, raw in enumerate(reader, start=2):
        if n - 1 > MAX_ROWS:
            raise ValueError(f"Too many rows: the limit is {MAX_ROWS:,} bookings per upload.")
        if not any(str(v or "").strip() for v in raw.values()):
            continue
        values, problems = {}, []
        for name, field in fields.items():
            if name not in header or name in ("created_at", "updated_at"):
                continue
            try:
                values[name] = _field_value(field, _unescape(raw.get(name)))
            except ValueError as exc:
                problems.append(str(exc))
        for c in REQUIRED:
            if not str(values.get(c) or "").strip():
                problems.append(f"{c} is empty")
        if values.get("date") and not _DATE_RE.match(str(values["date"])):
            problems.append("date must look like 2026-10-31")
        ref = str(values.get("ref_code") or "").strip().upper()
        if ref:
            if ref in existing:
                skipped.append((n, ref, "a booking with this reference already exists"))
                continue
            if ref in seen:
                skipped.append((n, ref, "this reference appears twice in the file"))
                continue
        if problems:
            errors.append((n, "; ".join(problems)))
            continue
        if not ref:
            ref = f"ISALU-{uuid.uuid4().hex[:10].upper()}"
        seen.add(ref)
        values["ref_code"] = ref
        if "is_active" not in header:
            values["is_active"] = True
        created = _unescape(raw.get("created_at")) if "created_at" in header else ""
        values["_created_at"] = created
        rows.append(values)
    return {"rows": rows, "errors": errors, "skipped": skipped, "columns": header, "unknown": unknown}


def _fill_doctor(values):
    from .models import Doctor
    doc_id = str(values.get("doctor_id") or "").strip()
    if not doc_id:
        return
    d = Doctor.objects.select_related("department").filter(doc_id__iexact=doc_id).first()
    if not d:
        return
    if not values.get("doctor_name"):
        values["doctor_name"] = getattr(d, "name", "") or getattr(d, "full_name", "") or ""
    if not values.get("doctor_specialty"):
        dept = getattr(d, "department", None)
        values["doctor_specialty"] = getattr(dept, "name", "") or getattr(d, "specialty", "") or ""


def import_rows(rows):
    """Create the bookings in one transaction. Returns how many were created."""
    created = 0
    with transaction.atomic():
        existing = set(Booking.objects.select_for_update().values_list("ref_code", flat=True))
        objs = []
        stamps = []
        for values in rows:
            values = dict(values)
            created_at = values.pop("_created_at", "")
            if values["ref_code"] in existing:
                continue
            _fill_doctor(values)
            objs.append(Booking(**values))
            stamps.append(created_at)
        Booking.objects.bulk_create(objs, batch_size=500)
        created = len(objs)
        # Keep the original booking times when the file has them.
        for obj, stamp in zip(objs, stamps):
            dt = parse_datetime(stamp) if stamp else None
            if dt:
                Booking.objects.filter(ref_code=obj.ref_code).update(
                    created_at=timezone.make_aware(dt) if timezone.is_naive(dt) else dt)
    return created


def _stage(rows, summary):
    token = secrets.token_urlsafe(24)
    import json
    path = os.path.join(tools_dir(), "staged", f"{token}.json")
    payload = {"created": timezone.now().isoformat(), "summary": summary,
               "rows": [{k: (str(v) if isinstance(v, (Decimal, datetime.datetime)) else v) for k, v in r.items()} for r in rows]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return token


def _unstage(token):
    import json
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token or ""):
        return None
    path = os.path.join(tools_dir(), "staged", f"{token}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    os.remove(path)
    created = parse_datetime(payload.get("created", ""))
    if not created or (timezone.now() - created).total_seconds() > STAGE_SECONDS:
        return None
    fields = {f.name: f for f in Booking._meta.concrete_fields}
    rows = []
    for r in payload["rows"]:
        values = {}
        for k, v in r.items():
            f = fields.get(k)
            if f is not None and v is not None and f.get_internal_type() == "DecimalField":
                v = Decimal(v)
            elif f is not None and v and f.get_internal_type() == "DateTimeField":
                v = parse_datetime(v)
            values[k] = v
        rows.append(values)
    return rows


def _clean_stage_dir():
    folder = os.path.join(tools_dir(), "staged")
    cutoff = timezone.now().timestamp() - STAGE_SECONDS
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def _render(request, **ctx):
    ctx.setdefault("message", "")
    ctx.setdefault("error", "")
    base = {
        "password_set": password_is_set(),
        "total": Booking.objects.count(),
        "upcoming": upcoming_queryset().count(),
        "log": _recent_log(),
        "phrase": CONFIRM_PHRASE,
        "required": ", ".join(REQUIRED),
        "max_rows": f"{MAX_ROWS:,}",
        "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "columns": ", ".join(export_columns()),
        "locked": _locked(request),
        "backups": backups(),
        "twofa": bool(twofa_record(request.user)),
    }
    base.update(ctx)
    response = render(request, "superuser_tools/page.html", base)
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@never_cache
@csrf_protect
def tools_page(request):
    user = request.user
    is_super = user.is_authenticated and user.is_active and user.is_superuser
    if user.is_authenticated and not is_super:
        raise Http404()  # staff or other signed-in users: the page doesn't exist for them

    # Signed in to /admin/ is not enough: this page needs its own password + authenticator sign-in.
    if not (is_super and _verified(request)):
        error = ""
        if request.method == "POST" and request.POST.get("action") == "signin":
            if _locked(request):
                error = "Too many attempts. Try again in 15 minutes."
            else:
                u = authenticate(request, username=request.POST.get("username", ""), password=request.POST.get("password", ""))
                if u is not None and u.is_active and u.is_superuser:
                    if not twofa_record(u):
                        # First sign-in: set up the authenticator app on the next screen.
                        if not password_is_set():
                            error = "Set the tools password on the server first: python manage.py set_tools_password"
                        else:
                            secret = totp.new_secret()
                            request.session[ENROL_FLAG] = {"user": u.pk, "secret": secret, "at": timezone.now().timestamp()}
                            _audit(request, "authenticator setup started", u.get_username())
                            return _enrol_page(request, u, secret)
                    elif _code_ok(request, u, request.POST.get("otp_code", "")):
                        login(request, u)
                        request.session[SESSION_FLAG] = {"user": u.pk, "at": timezone.now().timestamp()}
                        _audit(request, "sign in")
                        _alert(request, "Sign-in", "Someone signed in to the bookings tools page.")
                        return redirect(request.path)
                    else:
                        error = "Sign-in failed."
                else:
                    _fail(request)
                    error = "Sign-in failed."
                if _locked(request):
                    _alert(request, "Locked after failed sign-ins", "The bookings tools page was locked for 15 minutes after repeated failed sign-ins.")
        if request.method == "POST" and request.POST.get("action") == "enrol":
            pending = request.session.get(ENROL_FLAG) or {}
            from django.contrib.auth.models import User
            u = User.objects.filter(pk=pending.get("user"), is_active=True, is_superuser=True).first()
            fresh = pending and timezone.now().timestamp() - float(pending.get("at", 0)) <= ENROL_SECONDS
            if not (u and fresh and pending.get("secret")):
                request.session.pop(ENROL_FLAG, None)
                error = "The setup timed out. Sign in again to get a new QR code."
            elif _locked(request):
                error = "Too many attempts. Try again in 15 minutes."
            elif twofa_record(u):
                request.session.pop(ENROL_FLAG, None)
                error = "The authenticator is already set up for this account. Sign in with a code."
            elif not _password_ok(request, request.POST.get("tools_password", "")):
                _audit(request, "authenticator setup: wrong tools password", u.get_username())
                return _enrol_page(request, u, pending["secret"], "Wrong tools password.")
            else:
                counter = totp.verify(pending["secret"], request.POST.get("otp_code", ""))
                if counter is None:
                    _fail(request)
                    return _enrol_page(request, u, pending["secret"],
                                       "That code doesn't match. Check the phone's time is set automatically, then type the code the app shows now.")
                AppSetting.objects.update_or_create(key=TWOFA_KEY.format(u.pk), defaults={"value": {
                    "secret": pending["secret"], "confirmed_at": timezone.now().isoformat(), "last_counter": counter}})
                request.session.pop(ENROL_FLAG, None)
                _clear_fail(request)
                login(request, u, backend="django.contrib.auth.backends.ModelBackend")
                request.session[SESSION_FLAG] = {"user": u.pk, "at": timezone.now().timestamp()}
                _audit(request, "authenticator set up", u.get_username())
                _alert(request, "Authenticator set up", f"An authenticator app was set up for {u.get_username()} on the bookings tools page.")
                return redirect(request.path)
        response = render(request, "superuser_tools/signin.html", {"error": error, "locked": _locked(request)})
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response

    if request.method != "POST":
        return _render(request)

    action = request.POST.get("action", "")
    if action == "signout":
        _audit(request, "sign out")
        request.session.pop(SESSION_FLAG, None)
        logout(request)
        return redirect(request.path)

    # Every other action needs the tools password.
    if not password_is_set():
        return _render(request, error="Set the tools password first: python manage.py set_tools_password")
    if _locked(request):
        return _render(request, error="Too many wrong passwords. The tools are locked for 15 minutes.")
    if not _password_ok(request, request.POST.get("tools_password", "")):
        _audit(request, "wrong tools password", action)
        if _locked(request):
            _alert(request, "Locked after wrong passwords", "The bookings tools page was locked for 15 minutes after repeated wrong tools passwords.")
        return _render(request, error="Wrong tools password. Nothing was done.")
    if not _code_ok(request, request.user, request.POST.get("otp_code", "")):
        _audit(request, "wrong authenticator code", action)
        if _locked(request):
            _alert(request, "Locked after wrong codes", "The bookings tools page was locked for 15 minutes after repeated wrong authenticator codes.")
        return _render(request, error="Wrong or already-used authenticator code. Wait for the next code in your app. Nothing was done.")

    if action == "download_all":
        _audit(request, "download all bookings", f"{Booking.objects.count()} bookings")
        _alert(request, "All bookings downloaded", f"All bookings ({Booking.objects.count()}) were downloaded as CSV.")
        return _download(Booking.objects.order_by("date", "time"), f"all-bookings-{timezone.localdate().isoformat()}.csv")

    if action == "download_upcoming":
        qs = upcoming_queryset()
        _audit(request, "download upcoming bookings", f"{qs.count()} bookings")
        _alert(request, "Upcoming bookings downloaded", f"Upcoming bookings ({qs.count()}) were downloaded as CSV.")
        return _download(qs, f"upcoming-bookings-{timezone.localdate().isoformat()}.csv")

    if action == "template":
        response = HttpResponse("﻿" + ",".join(export_columns()) + "\r\n", content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="bookings-upload-template.csv"'
        return response

    if action == "preview":
        f = request.FILES.get("csv_file")
        if not f:
            return _render(request, error="Choose a CSV file.")
        name = (f.name or "").lower()
        ctype = (getattr(f, "content_type", "") or "").lower()
        if not name.endswith(".csv") or ctype not in ("text/csv", "application/csv", "text/plain", "application/vnd.ms-excel",
                                                    "text/comma-separated-values", "application/octet-stream", ""):
            _audit(request, "upload rejected", f"{f.name} ({ctype})")
            return _render(request, error="Only .csv files can be uploaded. Save the sheet as CSV (comma separated) and try again.")
        if f.size > MAX_UPLOAD_BYTES:
            return _render(request, error=f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        try:
            result = parse_upload(f.read())
        except UnicodeDecodeError:
            return _render(request, error="The file isn't UTF-8 text. Save it as 'CSV UTF-8' and try again.")
        except (ValueError, csv.Error) as exc:
            return _render(request, error=f"Couldn't read the CSV: {exc}")
        _clean_stage_dir()
        summary = {"file": f.name, "valid": len(result["rows"]), "errors": len(result["errors"]), "skipped": len(result["skipped"])}
        token = _stage(result["rows"], summary) if result["rows"] else ""
        _audit(request, "upload preview", f"{f.name}: {summary['valid']} valid, {summary['errors']} with errors, {summary['skipped']} skipped")
        return _render(request, preview={
            "file": f.name, "token": token, "valid": len(result["rows"]),
            "errors": result["errors"][:100], "error_count": len(result["errors"]),
            "skipped": result["skipped"][:100], "skipped_count": len(result["skipped"]),
            "unknown": result["unknown"],
            "sample": [{"ref": r["ref_code"], "name": r.get("patient_name", ""), "date": r.get("date", ""), "time": r.get("time", ""),
                        "doctor": r.get("doctor_name", "") or r.get("doctor_id", ""), "type": r.get("payment_type", "")}
                       for r in result["rows"][:10]],
        })

    if action == "import":
        # Password checked again above; the staged preview is single-use and expires in 15 minutes.
        if request.POST.get("confirm") != "yes":
            return _render(request, error="Import cancelled.")
        rows = _unstage(request.POST.get("token", ""))
        if rows is None:
            return _render(request, error="This preview has expired or was already used. Upload the file again.")
        n = import_rows(rows)
        from .views import broadcast_bulk_refresh
        broadcast_bulk_refresh("BULK_UPDATE", f"{n} bookings imported.")
        _audit(request, "bulk upload", f"{n} bookings created")
        _alert(request, "Bookings uploaded", f"{n} booking(s) were added to the database from a CSV upload.")
        return _render(request, message=f"Imported {n:,} booking(s).")

    if action == "delete_all":
        if request.POST.get("phrase", "").strip() != CONFIRM_PHRASE:
            return _render(request, error=f'Type {CONFIRM_PHRASE} exactly to remove all bookings. Nothing was removed.')
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(tools_dir(), "backups", f"bookings-backup-{stamp}.csv")
        cols = export_columns(include_all=True)
        with transaction.atomic():
            qs = Booking.objects.select_for_update().order_by("date", "time")
            count = qs.count()
            with open(backup, "w", encoding="utf-8", newline="") as fh:
                for chunk in _csv_rows(qs, cols):
                    fh.write(chunk)
            Booking.objects.all().delete()
        from .views import broadcast_bulk_refresh
        broadcast_bulk_refresh("BULK_UPDATE", "All bookings were removed.")
        _audit(request, "remove all bookings", f"{count} bookings removed; backup {os.path.basename(backup)}")
        _alert(request, "ALL BOOKINGS REMOVED", f"All {count} bookings were removed from the database. A backup was saved on the server as {os.path.basename(backup)}.")
        return _render(request, message=f"Removed {count:,} booking(s). A full backup was saved first.",
                       backup=os.path.basename(backup))

    if action == "download_backup":
        name = os.path.basename(request.POST.get("name", ""))
        path = os.path.join(tools_dir(), "backups", name)
        if not re.fullmatch(r"bookings-backup-\d{8}-\d{6}\.csv", name) or not os.path.exists(path):
            return _render(request, error="That backup doesn't exist.")
        _audit(request, "download backup", name)
        _alert(request, "Backup downloaded", f"The backup {name} was downloaded.")
        return FileResponse(open(path, "rb"), as_attachment=True, filename=name, content_type="text/csv")

    return _render(request, error="Unknown action.")


def backups():
    folder = os.path.join(tools_dir(), "backups")
    names = sorted((n for n in os.listdir(folder) if n.endswith(".csv")), reverse=True)
    return names[:10]
