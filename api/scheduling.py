"""
Single source of truth for specialist duty-day matching and daily capacity.

Before this module existed, BookingSerializer.validate(), the
/doctors/<id>/available-dates/ endpoint, /bookings/availability/ and the
clinic analytics each parsed duty days differently, so a date could be shown
as available and then rejected at booking time (or the reverse).

Supported schedule formats
--------------------------
Weekly (Create Weekly Schedule form, seed data):
    duty_days   = ["Mon", "Wed"]                 (or "Monday", ...)
    day_configs = {"Mon": {"shiftTimes": [...], "capacity": 15}}

Nth-weekday recurrence (Specific Date / Recurring form):
    duty_days   = ["Sun"]
    day_configs = {"Sun": {"shiftTimes": [...], "capacity": 10, "weeks": [1, 3]}}

One-off date:
    duty_days   = ["2026-11-08"]
    day_configs = {"2026-11-08": {"shiftTimes": [...], "capacity": 10, "date": "2026-11-08"}}

Legacy labels already stored in the database are also understood, e.g.
    "📅 1ST & 3RD SUNDAYS", "📅 EVERY SATURDAY", "1ST – 3RD MONDAYS",
    "📅 Sun, Jun 7, 2026 (1ST & 3RD SUNDAYS)", "ON-DUTY (SUNDAY)".
"""

import datetime
import re

from django.db.models import Q


# ============================================================
# CONSTANTS
# ============================================================

DEFAULT_DAILY_CAPACITY = 15

WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_WEEKDAY_WORDS = {
    0: ("mon", "monday", "mondays"),
    1: ("tue", "tues", "tuesday", "tuesdays"),
    2: ("wed", "weds", "wednesday", "wednesdays"),
    3: ("thu", "thur", "thurs", "thursday", "thursdays"),
    4: ("fri", "friday", "fridays"),
    5: ("sat", "saturday", "saturdays"),
    6: ("sun", "sunday", "sundays"),
}
WEEKDAY_ALIASES = {
    word: idx for idx, words in _WEEKDAY_WORDS.items() for word in words
}

_ORDINAL_WORDS = {
    "1st": 1, "first": 1,
    "2nd": 2, "second": 2,
    "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4,
    "5th": 5, "fifth": 5,
}
_ORDINAL_PATTERN = r"(1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth)"

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_LONG_DATE_RE = re.compile(
    r"(?:\b[a-z]{3,9},?\s+)?\b([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    _ORDINAL_PATTERN + r"\s*(?:-|–|—|to|through|thru)\s*" + _ORDINAL_PATTERN,
    re.IGNORECASE,
)

# Booking statuses that must NOT consume a doctor's daily capacity.
INACTIVE_BOOKING_STATUSES = (
    "disabled",
    "cancelled",
    "canceled",
    "rejected",
    "declined",
    "deleted",
    "expired",
    "void",
)


# ============================================================
# SMALL HELPERS
# ============================================================

def safe_int(value, default=0, minimum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    return result


def week_of_month(date_val):
    """1 for days 1-7, 2 for 8-14, ... 5 for 29-31."""
    return (date_val.day - 1) // 7 + 1


def parse_date(value):
    """Parse YYYY-MM-DD (and a few display formats) into a date, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value

    raw = str(value).strip()
    for fmt in (
        "%Y-%m-%d",
        "%A, %B %d, %Y",
        "%a, %b %d, %Y",
        "%A, %b %d, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_time_of_day(value):
    """
    Parse '09:30 AM', '9:30am', '14:00', '08:00 AM – 02:00 PM' (first time
    wins) into a datetime.time, else None.
    """
    if not value:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", str(value), re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = (match.group(3) or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime.time(hour, minute)


def normalize_weeks(value):
    """
    Accepts [1, 3], ["1", "3"], ["1st Week", "3rd Week"], "1,3" and returns a
    sorted list of unique ints in 1..5.
    """
    if value in (None, "", []):
        return []
    if isinstance(value, (int, float)):
        value = [value]
    if isinstance(value, str):
        value = re.split(r"[,&/ ]+", value)

    weeks = set()
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            n = int(item)
        else:
            text = str(item).strip().lower()
            n = None
            for word, num in _ORDINAL_WORDS.items():
                if text.startswith(word):
                    n = num
                    break
            if n is None:
                digits = re.match(r"(\d+)", text)
                n = int(digits.group(1)) if digits else None
        if n is not None and 1 <= n <= 5:
            weeks.add(n)
    return sorted(weeks)


def normalize_duty_days(value):
    """Accept list, JSON-ish string or comma string; return list of strings."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            import json
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    value = parsed
            except ValueError:
                value = [text]
        else:
            value = [p for p in re.split(r"[,|]", text)]
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for item in value:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def normalize_day_configs(value):
    """Accept dict or JSON string; return a cleaned dict of dicts."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}

    cleaned = {}
    for key, cfg in value.items():
        if not isinstance(cfg, dict):
            continue
        item = dict(cfg)
        shift_times = item.get("shiftTimes")
        if shift_times is None:
            shift_times = item.get("shift_times")
        if shift_times is None and item.get("time"):
            shift_times = [item.get("time")]
        if isinstance(shift_times, str):
            shift_times = [shift_times]
        if isinstance(shift_times, list):
            item["shiftTimes"] = [str(t).strip() for t in shift_times if str(t).strip()]
        if "capacity" in item and item["capacity"] not in (None, ""):
            item["capacity"] = safe_int(item["capacity"], DEFAULT_DAILY_CAPACITY, 0)
        if "weeks" in item:
            weeks = normalize_weeks(item.get("weeks"))
            # Keep unrecognised input (e.g. [7]) so validation can reject it
            # instead of silently turning a recurring day into "every week".
            if weeks or item.get("weeks") in (None, "", []):
                item["weeks"] = weeks
        if item.get("date"):
            parsed = parse_date(item["date"])
            item["date"] = parsed.isoformat() if parsed else ""
        cleaned[str(key).strip()] = item
    return cleaned


# ============================================================
# DUTY TOKEN PARSING
# ============================================================

class DutyRule:
    __slots__ = ("dates", "weekdays", "weeks")

    def __init__(self):
        self.dates = set()     # exact calendar dates
        self.weekdays = set()  # 0=Mon .. 6=Sun
        self.weeks = None      # None = every week, else set of 1..5

    def matches(self, date_val):
        if date_val in self.dates:
            return True
        if not self.weekdays:
            return False
        if date_val.weekday() not in self.weekdays:
            return False
        if self.weeks is None:
            return True
        return week_of_month(date_val) in self.weeks


def parse_duty_token(token):
    rule = DutyRule()
    text = str(token or "").strip()
    if not text:
        return rule

    # Exact dates first, then strip them so their weekday prefix
    # ("Sun, Jun 7, 2026") does not become an "every Sunday" rule.
    for y, m, d in _ISO_DATE_RE.findall(text):
        try:
            rule.dates.add(datetime.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    text = _ISO_DATE_RE.sub(" ", text)

    def _long_date(match):
        month = _MONTHS.get(match.group(1).lower())
        if month:
            try:
                rule.dates.add(
                    datetime.date(int(match.group(3)), month, int(match.group(2)))
                )
                return " "
            except ValueError:
                pass
        return match.group(0)

    text = _LONG_DATE_RE.sub(_long_date, text)
    lower = text.lower()

    for word in re.findall(r"[a-z]+", lower):
        if word in WEEKDAY_ALIASES:
            rule.weekdays.add(WEEKDAY_ALIASES[word])

    if not rule.weekdays:
        return rule

    if "every" in lower or "all weeks" in lower:
        rule.weeks = None
        return rule

    range_match = _RANGE_RE.search(lower)
    if range_match:
        start = _ORDINAL_WORDS[range_match.group(1).lower()]
        end = _ORDINAL_WORDS[range_match.group(2).lower()]
        if start <= end:
            rule.weeks = set(range(start, end + 1))
            return rule

    found = {
        _ORDINAL_WORDS[w.lower()]
        for w in re.findall(r"\b" + _ORDINAL_PATTERN + r"\b", lower)
    }
    rule.weeks = found or None
    return rule


def _duty_tokens(duty_days):
    """Split stored duty-day entries into individual tokens."""
    tokens = []
    for item in normalize_duty_days(duty_days):
        has_date = bool(_ISO_DATE_RE.search(item) or _LONG_DATE_RE.search(item))
        has_pattern = bool(re.search(r"\b" + _ORDINAL_PATTERN + r"\b|every", item, re.I))
        # "Mon, Wed, Fri" -> three tokens; never split dated or
        # ordinal labels, whose commas are part of the label.
        if not has_date and not has_pattern and re.search(r"[,/|]", item):
            tokens.extend(p.strip() for p in re.split(r"[,/|]", item) if p.strip())
        else:
            tokens.append(item)
    return tokens


def _clean_label(token):
    return re.sub(r"^[^\w]+", "", str(token)).strip()


def _find_config(configs, date_val, token):
    """Locate the day_configs entry that applies to this token/date."""
    if not configs:
        return {}
    lookup = {str(k).strip().lower(): v for k, v in configs.items()}

    candidates = [
        date_val.isoformat(),
        str(token).strip(),
        _clean_label(token),
    ]
    paren = re.search(r"\(([^)]*)\)", str(token))
    if paren:
        candidates.append(paren.group(1))
    candidates += [
        WEEKDAY_SHORT[date_val.weekday()],
        date_val.strftime("%A"),
    ]
    for key in candidates:
        cfg = lookup.get(str(key).strip().lower())
        if isinstance(cfg, dict):
            return cfg
    return {}


# ============================================================
# SCHEDULE MATCHING
# ============================================================

def match_schedule(schedule, date_val):
    """
    Return the day config (dict, possibly empty) if this schedule covers
    date_val, otherwise None.
    """
    configs = schedule.day_configs if isinstance(schedule.day_configs, dict) else {}
    tokens = _duty_tokens(schedule.duty_days)

    if not tokens:
        # Legacy rows with no duty days: fall back to config keys,
        # and finally treat the schedule as daily (previous behaviour).
        tokens = list(configs.keys())
        if not tokens:
            return {}

    occurrence = week_of_month(date_val)
    for token in tokens:
        rule = parse_duty_token(token)
        if not rule.matches(date_val):
            continue
        cfg = _find_config(configs, date_val, token)
        cfg_weeks = normalize_weeks(cfg.get("weeks"))
        if cfg_weeks and occurrence not in cfg_weeks:
            continue
        cfg_date = parse_date(cfg.get("date"))
        if cfg_date and cfg_date != date_val:
            continue
        return cfg
    return None


def config_capacity(schedule, cfg):
    base = safe_int(schedule.capacity, DEFAULT_DAILY_CAPACITY, 0)
    if isinstance(cfg, dict) and cfg.get("capacity") not in (None, ""):
        return safe_int(cfg.get("capacity"), base, 0)
    return base


def config_shift_label(schedule, cfg):
    if isinstance(cfg, dict):
        times = cfg.get("shiftTimes") or cfg.get("shift_times") or cfg.get("time")
        if isinstance(times, str):
            times = [times]
        if times:
            return ", ".join(str(t) for t in times if t)
    return schedule.shift_time or ""


def _active_schedules(doctor):
    cache = getattr(doctor, "_prefetched_objects_cache", None) or {}
    if "schedules" in cache:
        schedules = list(cache["schedules"])
    else:
        schedules = list(doctor.schedules.all())
    schedules = [s for s in schedules if s.status]
    schedules.sort(key=lambda s: s.sched_id)
    return schedules


def _doctor_exceptions(doctor):
    """
    {date_iso: exception} for both the original and new dates of a doctor's
    schedule exceptions. Cached on the instance so the 90-day availability
    scan costs one query.
    """
    cache = getattr(doctor, "_schedule_exception_cache", None)
    if cache is not None:
        return cache
    cache = {"original": {}, "new": {}}
    try:
        for exc in doctor.schedule_exceptions.all():
            cache["original"][exc.original_date] = exc
            if exc.action == "reschedule" and exc.new_date:
                cache["new"][exc.new_date] = exc
    except Exception:
        pass
    doctor._schedule_exception_cache = cache
    return cache


def clear_exception_cache(doctor):
    if hasattr(doctor, "_schedule_exception_cache"):
        del doctor._schedule_exception_cache


def resolve_doctor_day(doctor, date_val, ignore_exceptions=False):
    """
    Resolve whether a doctor is on duty on date_val and the capacity that
    applies. Every endpoint and the booking validator use this function.

    One-off schedule exceptions take priority over the regular schedule:
    a cancelled or moved-away date is closed, and a moved-to date is open
    with the original day's capacity and hours.
    """
    date_val = parse_date(date_val)
    result = {
        "on_duty": False,
        "capacity": 0,
        "schedule": None,
        "config": {},
        "shift": "",
        "has_schedule": False,
        "reason": "",
        "exception": None,
        "note": "",
    }
    if doctor is None or date_val is None:
        result["reason"] = "invalid"
        return result

    if not ignore_exceptions:
        exceptions = _doctor_exceptions(doctor)
        iso = date_val.isoformat()
        moved_in = exceptions["new"].get(iso)
        if moved_in is not None:
            shift = ", ".join(moved_in.shift_times or [])
            result.update(
                on_duty=True,
                capacity=safe_int(moved_in.capacity, DEFAULT_DAILY_CAPACITY, 0),
                schedule=(_active_schedules(doctor) or [None])[0],
                config={"shiftTimes": list(moved_in.shift_times or [])},
                shift=shift,
                has_schedule=True,
                reason="moved_in",
                exception=moved_in,
                note=f"Clinic moved here from {moved_in.original_date}",
            )
            return result
        moved_out = exceptions["original"].get(iso)
        if moved_out is not None:
            if moved_out.action == "reschedule":
                note = f"Clinic moved to {moved_out.new_date}"
            else:
                note = "Clinic cancelled" + (f": {moved_out.reason}" if moved_out.reason else "")
            result.update(
                has_schedule=True,
                reason="cancelled" if moved_out.action == "cancel" else "moved_out",
                exception=moved_out,
                note=note,
            )
            return result

    schedules = _active_schedules(doctor)
    if not schedules:
        result["reason"] = "no_active_schedule"
        return result

    result["has_schedule"] = True
    for schedule in schedules:
        cfg = match_schedule(schedule, date_val)
        if cfg is None:
            continue
        result.update(
            on_duty=True,
            capacity=config_capacity(schedule, cfg),
            schedule=schedule,
            config=cfg,
            shift=config_shift_label(schedule, cfg),
        )
        return result

    first = schedules[0]
    result.update(
        capacity=safe_int(first.capacity, DEFAULT_DAILY_CAPACITY, 0),
        schedule=first,
        reason="not_on_duty",
    )
    return result


def compute_weekly_capacity(duty_days, day_configs, capacity):
    """Sum of per-duty-day capacities (falls back to the default capacity)."""
    base = safe_int(capacity, DEFAULT_DAILY_CAPACITY, 0)
    configs = day_configs if isinstance(day_configs, dict) else {}
    lookup = {str(k).strip().lower(): v for k, v in configs.items()}
    tokens = _duty_tokens(duty_days)
    if not tokens:
        return base

    total = 0
    for token in tokens:
        cfg = lookup.get(str(token).strip().lower()) or lookup.get(
            _clean_label(token).lower()
        ) or {}
        if isinstance(cfg, dict) and cfg.get("capacity") not in (None, ""):
            total += safe_int(cfg.get("capacity"), base, 0)
        else:
            total += base
    return total


def describe_rule_errors(duty_days, day_configs):
    """
    Return a list of duty-day tokens that cannot be interpreted, so the API
    can reject them instead of saving a schedule nobody can ever book.
    """
    bad = []
    configs = day_configs if isinstance(day_configs, dict) else {}
    for token in _duty_tokens(duty_days):
        rule = parse_duty_token(token)
        if rule.dates or rule.weekdays:
            continue
        cfg = _find_config(configs, datetime.date.today(), token)
        if parse_date(cfg.get("date")):
            continue
        bad.append(token)
    return bad


# ============================================================
# BOOKING COUNTS
# ============================================================

def inactive_status_q():
    q = Q()
    for value in INACTIVE_BOOKING_STATUSES:
        q |= Q(status__iexact=value)
    return q


def capacity_bookings_qs(queryset=None):
    """Bookings that consume a doctor's daily capacity."""
    if queryset is None:
        from .models import Booking
        queryset = Booking.objects.all()
    return queryset.filter(is_active=True).exclude(inactive_status_q())


def count_doctor_bookings(doctor_id, date_val, exclude_ref=None):
    date_val = parse_date(date_val)
    if not doctor_id or date_val is None:
        return 0
    qs = capacity_bookings_qs().filter(
        doctor_id=str(doctor_id), date=date_val.isoformat()
    )
    if exclude_ref:
        qs = qs.exclude(ref_code=exclude_ref)
    return qs.count()


# ============================================================
# CANONICAL SCHEDULE JSON
# ============================================================
#
# Normal (every week):
#     duty_days   = ["Thu"]
#     day_configs = {"Thu": {"shiftTimes": ["04:00 PM – 06:00 PM"], "capacity": 6}}
#
# Recurring (selected weeks of the month):
#     duty_days   = ["Sat"]
#     day_configs = {"Sat": {"shiftTimes": ["02:00 PM – 04:00 PM"], "capacity": 12, "weeks": [1, 3]}}

class ScheduleFormatError(ValueError):
    pass


def weekday_key(value):
    """'Thursday', 'thu', 'THURS' -> 'Thu'; anything else -> None."""
    word = re.sub(r"[^a-z]", "", str(value or "").strip().lower())
    idx = WEEKDAY_ALIASES.get(word)
    return WEEKDAY_SHORT[idx] if idx is not None else None


def is_weekday_only(duty_days, day_configs):
    keys = list(normalize_duty_days(duty_days)) + list((day_configs or {}).keys())
    return bool(keys) and all(weekday_key(k) for k in keys)


def canonicalize_schedule(duty_days, day_configs, default_capacity, default_shift):
    """
    Return (duty_days, day_configs) in the canonical format above.

    - Day names are normalised to Mon..Sun and ordered Mon -> Sun.
    - Every duty day gets a config; every config key is a duty day.
    - "weeks" is kept only for recurring days (a subset of 1..5); selecting
      all five weeks is the same as every week, so it is dropped.
    - Unknown keys (e.g. "date", "time") are removed.
    """
    configs = day_configs if isinstance(day_configs, dict) else {}
    by_day = {}
    for key, cfg in configs.items():
        day = weekday_key(key)
        if not day:
            raise ScheduleFormatError(
                f"'{key}' is not a weekday. Use Mon, Tue, Wed, Thu, Fri, Sat or Sun."
            )
        by_day[day] = cfg if isinstance(cfg, dict) else {}

    days = set(by_day)
    for item in normalize_duty_days(duty_days):
        day = weekday_key(item)
        if not day:
            raise ScheduleFormatError(
                f"'{item}' is not a weekday. Use Mon, Tue, Wed, Thu, Fri, Sat or Sun."
            )
        days.add(day)

    if not days:
        raise ScheduleFormatError("Select at least one duty day.")

    base_capacity = safe_int(default_capacity, DEFAULT_DAILY_CAPACITY, 1)
    ordered = [d for d in WEEKDAY_SHORT if d in days]
    canonical = {}
    for day in ordered:
        cfg = by_day.get(day, {})

        times = cfg.get("shiftTimes")
        if times is None:
            times = cfg.get("shift_times") or cfg.get("time")
        if isinstance(times, str):
            times = [times]
        times = [str(t).strip() for t in (times or []) if str(t).strip()]
        if not times:
            times = [str(default_shift).strip()] if str(default_shift or "").strip() else []
        if not times:
            raise ScheduleFormatError(f"{day}: select at least one shift time.")

        capacity = safe_int(cfg.get("capacity"), base_capacity)
        if capacity < 1:
            raise ScheduleFormatError(f"{day}: capacity must be at least 1 patient.")

        entry = {"shiftTimes": times, "capacity": capacity}

        if "weeks" in cfg and cfg.get("weeks") not in (None, "", []):
            weeks = normalize_weeks(cfg.get("weeks"))
            if not weeks:
                raise ScheduleFormatError(f"{day}: weeks must be between 1 and 5.")
            if len(weeks) < 5:
                entry["weeks"] = weeks

        canonical[day] = entry

    return ordered, canonical
