"""
Booking Payments report: online booking payments and payouts read live
from the Paystack API, matched to the bookings they paid for.

Paystack is the source of truth here. Every successful, failed or abandoned
transaction in the date range that belongs to a booking is listed, together
with the Paystack payout (settlement) that paid it out to the hospital's bank.

Amounts from Paystack are in kobo; everything returned here is in naira.
"""

import datetime
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import payments
from .models import Booking

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 92
PER_PAGE = 100
MAX_PAGES = 60                 # 6,000 records per list, far beyond a quarter of bookings
PAYOUT_LOOKAHEAD_DAYS = 10     # payments near the end of the range are paid out a few days later
CACHE_SECONDS = 120


class ReportError(Exception):
    """A problem staff can act on (shown in the dashboard)."""


# ---------------------------------------------------------------------------
# Paystack access (staff-facing errors)
# ---------------------------------------------------------------------------

def _get(path, params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{settings.PAYSTACK_API_URL}{path}?{query}"
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {payments.paystack_secret_key()}",
        "Accept": "application/json",
        "User-Agent": "IsaluBooking/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode() or "{}").get("message")
        except Exception:
            message = None
        logger.warning("Paystack GET %s failed: %s %s", path, exc.code, message)
        if exc.code == 401:
            raise ReportError(
                "Paystack rejected the secret key in backend/.env. Run 'python manage.py check_payments' "
                "to find the problem, then restart the backend."
            )
        raise ReportError(f"Paystack answered with an error ({exc.code}): {message or 'no details'}. Try again shortly.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Paystack GET %s unreachable: %s", path, exc)
        raise ReportError("Couldn't reach Paystack. Check the server's internet connection and try again.")
    if not payload.get("status"):
        raise ReportError(payload.get("message") or "Paystack could not return the records.")
    return payload.get("data") or [], payload.get("meta") or {}


def _get_all(path, params):
    """Every page of a Paystack list endpoint. Returns (rows, truncated)."""
    rows = []
    page = 1
    while page <= MAX_PAGES:
        data, meta = _get(path, {**params, "perPage": PER_PAGE, "page": page})
        rows.extend(data if isinstance(data, list) else [])
        page_count = int(meta.get("pageCount") or 0)
        if page_count:
            if page >= page_count:
                return rows, False
        elif len(data or []) < PER_PAGE:
            return rows, False
        page += 1
    return rows, True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def naira(kobo):
    try:
        return round(float(kobo or 0) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _local_date(value):
    if not value:
        return None
    dt = parse_datetime(str(value)) if not isinstance(value, datetime.datetime) else value
    if dt is None:
        return str(value)[:10]
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)
    return timezone.localtime(dt).date().isoformat()


def _local_iso(value):
    if not value:
        return None
    dt = parse_datetime(str(value))
    if dt is None:
        return str(value)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)
    return timezone.localtime(dt).isoformat()


def _metadata(tx):
    meta = tx.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _booking_ref_from_tx(tx):
    ref_code = str(_metadata(tx).get("ref_code") or "").strip()
    if ref_code:
        return ref_code
    for field in _metadata(tx).get("custom_fields") or []:
        if isinstance(field, dict) and field.get("variable_name") == "ref_code" and field.get("value"):
            return str(field["value"]).strip()
    return ""


def is_booking_transaction(tx):
    """Payments started by the booking site (other payments on the account are left out)."""
    return bool(_booking_ref_from_tx(tx)) or str(tx.get("reference") or "").upper().startswith("ISALU-")


def parse_range(raw_from, raw_to):
    today = timezone.localdate()
    try:
        date_to = datetime.date.fromisoformat(str(raw_to)) if raw_to else today
        date_from = datetime.date.fromisoformat(str(raw_from)) if raw_from else date_to - datetime.timedelta(days=29)
    except ValueError:
        raise ReportError("Dates must look like 2026-10-21.")
    if date_from > date_to:
        raise ReportError("The start date is after the end date.")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise ReportError(f"Choose a range of {MAX_RANGE_DAYS} days or less.")
    return date_from, date_to


def _day_bounds_utc(date_from, date_to):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min), tz)
    end = timezone.make_aware(datetime.datetime.combine(date_to, datetime.time.max), tz)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return start.astimezone(datetime.timezone.utc).strftime(fmt), end.astimezone(datetime.timezone.utc).strftime(fmt)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def build_report(date_from, date_to, refresh=False):
    if not payments.paystack_enabled():
        raise ReportError(
            "Paystack isn't set up, so there are no online payments to show. Add PAYSTACK_SECRET_KEY to "
            "backend/.env and restart the backend (see PAYMENTS_SETUP.md)."
        )
    key = payments.paystack_secret_key()
    cache_key = f"paystack-report:{key[-6:]}:{date_from}:{date_to}"
    if not refresh:
        cached = cache.get(cache_key)
        if cached:
            return cached

    utc_from, utc_to = _day_bounds_utc(date_from, date_to)
    warnings = []

    # 1. transactions in the range (all statuses)
    txs, truncated = _get_all("/transaction", {"from": utc_from, "to": utc_to})
    if truncated:
        warnings.append("Paystack returned more records than can be shown at once; choose a shorter range.")
    booking_txs = [t for t in txs if is_booking_transaction(t)]
    other_count = len(txs) - len(booking_txs)

    # 2. payouts that can include these payments (settled up to a few days later)
    settle_to = min(date_to + datetime.timedelta(days=PAYOUT_LOOKAHEAD_DAYS), timezone.localdate())
    s_from, s_to = _day_bounds_utc(date_from, max(settle_to, date_from))
    settlements, s_truncated = _get_all("/settlement", {"from": s_from, "to": s_to})
    if s_truncated:
        warnings.append("Too many payouts to list; choose a shorter range.")

    # 3. which payout paid out which payment
    tx_ids = {str(t.get("id")) for t in booking_txs}
    refs = {str(t.get("reference")) for t in booking_txs}

    def settlement_transactions(settlement):
        try:
            rows, _ = _get_all(f"/settlement/{settlement.get('id')}/transactions", {})
            return settlement, rows, None
        except ReportError as exc:
            return settlement, [], str(exc)

    payout_of = {}
    payout_booking_stats = defaultdict(lambda: {"count": 0, "amount": 0.0, "fees": 0.0})
    payout_failed_lookups = 0
    if settlements:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for settlement, rows, err in pool.map(settlement_transactions, settlements):
                if err:
                    payout_failed_lookups += 1
                    continue
                for row in rows:
                    rid, rref = str(row.get("id")), str(row.get("reference"))
                    if rid in tx_ids or rref in refs or is_booking_transaction(row):
                        payout_of.setdefault(rref, settlement)
                        payout_of.setdefault(rid, settlement)
                        stats = payout_booking_stats[str(settlement.get("id"))]
                        stats["count"] += 1
                        stats["amount"] += naira(row.get("amount"))
                        stats["fees"] += naira(row.get("fees"))
    if payout_failed_lookups:
        warnings.append(f"Couldn't read the payments inside {payout_failed_lookups} payout(s); refresh to try again.")

    # 4. matching bookings (one query)
    ref_codes = {_booking_ref_from_tx(t) for t in booking_txs} - {""}
    bookings = {}
    for b in Booking.objects.filter(payment_reference__in=refs).only(*_BOOKING_FIELDS):
        bookings[b.payment_reference] = b
    by_code = {b.ref_code: b for b in Booking.objects.filter(ref_code__in=ref_codes).only(*_BOOKING_FIELDS)}

    transactions = []
    days = defaultdict(lambda: {
        "payments": 0, "gross": 0.0, "fees": 0.0, "net": 0.0, "paid_out": 0.0,
        "awaiting_payout": 0.0, "failed": 0, "abandoned": 0,
    })
    for tx in booking_txs:
        status = str(tx.get("status") or "").lower()
        amount, fees = naira(tx.get("amount")), naira(tx.get("fees"))
        paid_day = _local_date(tx.get("paid_at") or tx.get("paidAt")) if status == "success" else None
        created_day = _local_date(tx.get("created_at") or tx.get("createdAt"))
        day = paid_day or created_day
        settlement = payout_of.get(str(tx.get("reference"))) or payout_of.get(str(tx.get("id")))
        booking = bookings.get(str(tx.get("reference"))) or by_code.get(_booking_ref_from_tx(tx))

        auth = tx.get("authorization") or {}
        customer = tx.get("customer") or {}
        card = " ".join(x for x in [str(auth.get("card_type") or "").strip().upper(), f"•••• {auth['last4']}" if auth.get("last4") else ""] if x)
        payout = None
        if settlement:
            payout = {
                "id": settlement.get("id"),
                "status": str(settlement.get("status") or "").lower(),
                "date": _local_date(settlement.get("settlement_date") or settlement.get("settlementDate") or settlement.get("createdAt")),
            }
        transactions.append({
            "id": tx.get("id"),
            "reference": tx.get("reference"),
            "status": status,
            "amount": amount,
            "fees": fees,
            "net": round(amount - fees, 2) if status == "success" else 0.0,
            "currency": tx.get("currency") or "NGN",
            "channel": tx.get("channel") or auth.get("channel") or "",
            "card": card,
            "bank": auth.get("bank") or "",
            "gateway_response": tx.get("gateway_response") or "",
            "paid_at": _local_iso(tx.get("paid_at") or tx.get("paidAt")) if status == "success" else None,
            "created_at": _local_iso(tx.get("created_at") or tx.get("createdAt")),
            "day": day,
            "customer_email": customer.get("email") or "",
            "customer_name": " ".join(x for x in [customer.get("first_name"), customer.get("last_name")] if x),
            "ref_code": (booking.ref_code if booking else _booking_ref_from_tx(tx)) or "",
            "booking": _booking_payload(booking),
            "payout": payout,
        })

        bucket = days[day]
        if status == "success":
            bucket["payments"] += 1
            bucket["gross"] += amount
            bucket["fees"] += fees
            bucket["net"] += amount - fees
            if payout and payout["status"] == "success":
                bucket["paid_out"] += amount - fees
            else:
                bucket["awaiting_payout"] += amount - fees
        elif status == "abandoned":
            bucket["abandoned"] += 1
        elif status in ("failed", "reversed"):
            bucket["failed"] += 1

    transactions.sort(key=lambda t: t["paid_at"] or t["created_at"] or "", reverse=True)

    day_rows = []
    cursor = date_to
    while cursor >= date_from:
        d = days.get(cursor.isoformat())
        row = {"date": cursor.isoformat(), **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in (d or days.default_factory()).items()}}
        day_rows.append(row)
        cursor -= datetime.timedelta(days=1)

    payouts = []
    for s in settlements:
        sid = str(s.get("id"))
        stats = payout_booking_stats.get(sid, {"count": 0, "amount": 0.0, "fees": 0.0})
        payouts.append({
            "id": s.get("id"),
            "status": str(s.get("status") or "").lower(),
            "date": _local_date(s.get("settlement_date") or s.get("settlementDate") or s.get("createdAt")),
            "total_amount": naira(s.get("total_amount")),
            "total_fees": naira(s.get("total_fees")),
            "total_processed": naira(s.get("total_processed")),
            "deductions": naira(s.get("deductions")),
            "paid_out": naira(s.get("effective_amount")),
            "booking_payments": stats["count"],
            "booking_amount": round(stats["amount"], 2),
            "booking_fees": round(stats["fees"], 2),
            "booking_net": round(stats["amount"] - stats["fees"], 2),
            "currency": s.get("currency") or "NGN",
        })
    payouts.sort(key=lambda p: p["date"] or "", reverse=True)

    ok = [t for t in transactions if t["status"] == "success"]
    totals = {
        "payments": len(ok),
        "gross": round(sum(t["amount"] for t in ok), 2),
        "fees": round(sum(t["fees"] for t in ok), 2),
        "net": round(sum(t["net"] for t in ok), 2),
        "paid_out": round(sum(t["net"] for t in ok if t["payout"] and t["payout"]["status"] == "success"), 2),
        "awaiting_payout": round(sum(t["net"] for t in ok if not (t["payout"] and t["payout"]["status"] == "success")), 2),
        "failed": sum(1 for t in transactions if t["status"] in ("failed", "reversed")),
        "abandoned": sum(1 for t in transactions if t["status"] == "abandoned"),
        "unmatched": sum(1 for t in ok if not t["booking"]),
    }

    report = {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "mode": "live" if key.startswith("sk_live_") else "test",
        "fetched_at": timezone.localtime().isoformat(),
        "totals": totals,
        "days": day_rows,
        "payouts": payouts,
        "transactions": transactions,
        "other_transactions_excluded": other_count,
        "warnings": warnings,
    }
    cache.set(cache_key, report, CACHE_SECONDS)
    return report


_BOOKING_FIELDS = (
    "ref_code", "payment_reference", "patient_name", "patient_phone", "doctor_name",
    "doctor_specialty", "date", "time", "status", "payment_status", "payment_type",
)


def _booking_payload(b):
    if b is None:
        return None
    return {
        "ref_code": b.ref_code,
        "patient_name": b.patient_name,
        "patient_phone": b.patient_phone,
        "doctor": b.doctor_name,
        "clinic": b.doctor_specialty,
        "date": str(b.date),
        "time": b.time,
        "status": b.status,
        "payment_status": b.payment_status,
    }
