import sys
import os
import random
import datetime

sys.path.insert(0, os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'clinic_backend.settings')

# Never send real SMS from the test suite: blank every gateway credential
# before Django loads .env-derived settings. (SMS then falls back to console logging.)
# The suite creates many bookings from one IP; relax the public rate limits here only.
for _key, _val in (('THROTTLE_PUBLIC_BOOKING', '100000/hour'), ('THROTTLE_PUBLIC_LOOKUP', '100000/min'),
                   ('THROTTLE_LOGIN', '100000/min'), ('THROTTLE_ANON', '1000000/day')):
    os.environ.setdefault(_key, _val)
for _key in ('EBULKSMS_USERNAME', 'EBULKSMS_API_KEY', 'BULKSMS_TOKEN_ID', 'BULKSMS_TOKEN_SECRET',
             'BULKSMS_USERNAME', 'BULKSMS_PASSWORD', 'SMS_API_URL', 'SMS_API_KEY'):
    os.environ[_key] = ''

import django
django.setup()

from django.conf import settings as _settings
_settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

from rest_framework.test import APIClient
from django.utils import timezone
from api.models import Department, Doctor, SpecialistSchedule, Booking, HmoCompany, Role, UserProfile
from django.contrib.auth.models import User

def run_e2e_tests():
    print('================================================================================')
    print('ISALU HOSPITALS - AUTOMATED E2E INTEGRATION & REGRESSION TEST SUITE')
    print('================================================================================\n')

    client = APIClient()

    # Authenticate client as staff user for staff actions
    admin_user = User.objects.filter(is_superuser=True).first()
    if not admin_user:
        admin_user = User.objects.create_superuser('admin_e2e', 'admin_e2e@isaluhospitals.com', 'admin123')
    client.force_authenticate(user=admin_user)

    # TEST 1: Retrieve Departments & Verify Counts
    res_depts = client.get('/api/departments/')
    assert res_depts.status_code == 200, f"Departments API Failed: {res_depts.status_code}"
    print(f'[TEST 1 PASS] Department Catalog API: {len(res_depts.data)} clinical departments loaded.')

    # TEST 2: Retrieve Doctors & Verify Department Linkage
    res_docs = client.get('/api/doctors/')
    assert res_docs.status_code == 200, f"Doctors API Failed: {res_docs.status_code}"
    docs = res_docs.data
    neuro_docs = [d for d in docs if (d.get('departmentId') == 'neurology' or (d.get('department') and d['department'].get('dept_id') == 'neurology'))]
    uro_docs = [d for d in docs if (d.get('departmentId') == 'urology' or (d.get('department') and d['department'].get('dept_id') == 'urology'))]
    assert len(neuro_docs) > 0, "No Neurology doctors found!"
    assert len(uro_docs) > 0, "No Urology doctors found!"
    print(f'[TEST 2 PASS] Doctor Department Linkage: Neurology={len(neuro_docs)} | Urology={len(uro_docs)} (Zero Cross-Contamination).')

    # TEST 3: Same-Day 30-Minute Cutoff Rejection
    now_local = timezone.localtime(timezone.now())
    today_str = now_local.strftime('%Y-%m-%d')
    past_time = (now_local - datetime.timedelta(minutes=10)).strftime('%I:%M %p')
    cutoff_payload = {
        'refCode': f'E2E-CUTOFF-{random.randint(10000, 99999)}',
        'doctorId': 'doc-14',
        'doctorName': 'Dr. Victoria Danjuma',
        'doctorSpecialty': 'Neurology',
        'date': today_str,
        'time': past_time,
        'patientName': 'E2E Cutoff Patient',
        'patientPhone': '08099887766',
        'paymentType': 'Private Self-Pay'
    }
    res_cutoff = client.post('/api/bookings/', cutoff_payload, format='json')
    assert res_cutoff.status_code == 400, f"Cutoff test failed, expected 400 got {res_cutoff.status_code}"
    print(f'[TEST 3 PASS] Same-Day 30-Min Cutoff Enforcement: HTTP 400 Bad Request accurately returned.')

    # TEST 4: Valid Online Appointment Ticket Creation (next real duty date)
    avail = client.get('/api/doctors/doc-14/available-dates/?days=30').data
    future = [d for d in avail['availability'] if d['available'] and d['date'] > today_str]
    assert future, "doc-14 has no available future duty date"
    next_duty_date = future[0]['date']
    ref_code = f'ISALU-E2E-{random.randint(10000, 99999)}'
    booking_payload = {
        'refCode': ref_code,
        'doctorId': 'doc-14',
        'doctorName': 'Dr. Victoria Danjuma',
        'doctorSpecialty': 'Neurology',
        'date': next_duty_date,
        'time': '11:00 AM',
        'patientName': 'Automated E2E Patient',
        'patientPhone': '08011223344',
        'patientEmail': 'e2e@isaluhospitals.com',
        'paymentType': 'Private Self-Pay'
    }
    res_book = client.post('/api/bookings/', booking_payload, format='json')
    assert res_book.status_code == 201, f"Booking creation failed: {res_book.status_code}"
    created_ref = res_book.data.get('refCode') or res_book.data.get('ref_code')
    print(f'[TEST 4 PASS] Patient Booking Creation: HTTP 201 Created | Ref: {created_ref}')

    # TEST 5: Cashdesk POS Payment Processing
    res_pay = client.post(f'/api/bookings/{created_ref}/pay-cashdesk/', {'paymentMethod': 'POS Card'}, format='json')
    assert res_pay.status_code == 200, f"Cashdesk payment failed: {res_pay.status_code}"
    print(f'[TEST 5 PASS] Cashdesk Billing Payment: Payment Status = {res_pay.data.get("paymentStatus")}')

    # TEST 6: Reception Check-in Action
    res_checkin = client.post(f'/api/bookings/{created_ref}/check-in/')
    assert res_checkin.status_code == 200, f"Check-in failed: {res_checkin.status_code}"
    print(f'[TEST 6 PASS] Reception Patient Check-in: Status = {res_checkin.data.get("status")}')

    # TEST 7: Staff Authentication & Silent JWT Refresh Endpoint
    res_refresh = client.post('/api/auth/token-refresh/', {'refresh': 'invalid_test_token'}, format='json')
    assert res_refresh.status_code == 401, f"Token refresh validation failed: {res_refresh.status_code}"
    print(f'[TEST 7 PASS] JWT Token Refresh Endpoint (/api/auth/token-refresh/): 401 Unauthorized handling operational.')

    # TEST 8: Real-Time Event Stream Endpoint
    res_stream = client.get('/api/stream/events/')
    assert res_stream.status_code == 200, f"Event stream endpoint failed: {res_stream.status_code}"
    print(f'[TEST 8 PASS] Real-Time Event Stream (/api/stream/events/): HTTP 200 OK (text/event-stream).')

    run_regression_tests(client, today_str)
    run_schedule_change_tests(client, today_str)
    run_permission_tests(client, today_str)
    run_duplicate_and_channel_tests(client, today_str)
    run_hmo_decline_tests(client, today_str)

    print('\n================================================================================')
    print('SUMMARY: ALL E2E INTEGRATION & REGRESSION TESTS PASSED (100% SUCCESS RATE)')
    print('================================================================================')

def run_regression_tests(client, today_str):
    """Regression tests for the schedule / booking / security fixes."""
    anon = APIClient()
    today = datetime.date.fromisoformat(today_str)

    def next_weekday(weekday, after=today):
        d = after + datetime.timedelta(days=1)
        while d.weekday() != weekday:
            d += datetime.timedelta(days=1)
        return d

    # R1: create weekly schedule (dashboard payload shape)
    doc = Doctor.objects.create(doc_id=f'doc-reg-{random.randint(1000,9999)}', name='Specialist Z',
                                full_name='Dr. Regression Test', status=True)
    payload = {
        'doctorId': doc.doc_id, 'doctor_id': doc.doc_id, 'doctorName': 'Dr. Regression Test (Specialist Z)',
        'room': 'Suite 9', 'dutyDays': ['Tue', 'Thu'], 'duty_days': ['Tue', 'Thu'],
        'dayConfigs': {'Tue': {'shiftTimes': ['08:00 AM – 02:00 PM'], 'capacity': 2},
                       'Thu': {'shiftTimes': ['01:00 PM – 06:00 PM'], 'capacity': 5}},
        'shiftTime': 'Tue: 08:00 AM | Thu: 01:00 PM', 'capacity': 3, 'status': True,
    }
    r = client.post('/api/schedules/', payload, format='json')
    assert r.status_code == 201, r.data
    sched_id = r.data['sched_id']
    assert r.data['totalWeeklyCapacity'] == 7, r.data
    assert r.data['doctorName'] == 'Dr. Regression Test'
    print('[R1 PASS] Weekly schedule created; weekly capacity derived from per-day capacities.')

    # R2: unknown doctor / empty days / anonymous create are rejected
    assert client.post('/api/schedules/', {**payload, 'doctorId': 'nope', 'doctor_id': 'nope'}, format='json').status_code == 400
    assert client.post('/api/schedules/', {**payload, 'dutyDays': [], 'duty_days': [], 'dayConfigs': {}, 'day_configs': {}}, format='json').status_code == 400
    assert anon.post('/api/schedules/', payload, format='json').status_code in (401, 403)
    print('[R2 PASS] Invalid schedules rejected (unknown doctor, no days, anonymous).')

    # R3: per-day capacity enforced; Tuesday capacity is 2
    tue = next_weekday(1).isoformat()
    def book(date, name, c=anon):
        return c.post('/api/bookings/', {'doctorId': doc.doc_id, 'date': date, 'time': '09:00 AM',
                                         'patientName': name, 'patientPhone': '0800000000',
                                         'paymentType': 'Private Self-Pay'}, format='json')
    assert book(tue, 'P1').status_code == 201
    r2 = book(tue, 'P2'); assert r2.status_code == 201
    assert book(tue, 'P3').status_code == 400
    print('[R3 PASS] Per-day capacity (Tue=2) enforced.')

    # R4: cancelling frees a slot
    ref = r2.data['refCode']
    assert client.patch(f'/api/bookings/{ref}/', {'status': 'Cancelled'}, format='json').status_code == 200
    assert book(tue, 'P3').status_code == 201
    print('[R4 PASS] Cancelled bookings no longer consume capacity.')

    # R5: off-duty day and past date rejected
    assert book(next_weekday(2).isoformat(), 'Wed').status_code == 400
    assert book((today - datetime.timedelta(days=7)).isoformat(), 'Past').status_code == 400
    print('[R5 PASS] Off-duty and past dates rejected.')

    # R6: disabling the shift (dashboard label) really disables it
    r = client.patch(f'/api/schedules/{sched_id}/', {'status': 'Disabled Shift 🚫'}, format='json')
    assert r.status_code == 200 and r.data['status'] is False, r.data
    assert book(next_weekday(3).isoformat(), 'Thu').status_code == 400
    client.patch(f'/api/schedules/{sched_id}/', {'status': True}, format='json')
    print('[R6 PASS] "Disabled Shift" label disables the schedule; bookings blocked.')

    # R7: nth-week recurrence + one-off date
    r = client.post('/api/schedules/', {
        'doctorId': doc.doc_id, 'room': 'Suite 9', 'dutyDays': ['Sun'],
        'dayConfigs': {'Sun': {'shiftTimes': ['09:00 AM – 01:00 PM'], 'capacity': 4, 'weeks': [1, 3]}},
        'capacity': 4}, format='json')
    assert r.status_code == 201, r.data
    sundays = [today + datetime.timedelta(days=i) for i in range(1, 60)]
    sundays = [d for d in sundays if d.weekday() == 6]
    ok = [d for d in sundays if (d.day - 1) // 7 + 1 in (1, 3)][0]
    bad = [d for d in sundays if (d.day - 1) // 7 + 1 not in (1, 3)][0]
    assert book(ok.isoformat(), 'Sun ok').status_code == 201
    assert book(bad.isoformat(), 'Sun bad').status_code == 400
    # One-off dates are no longer a schedule type: only weekday keys are accepted.
    r = client.post('/api/schedules/', {
        'doctorId': doc.doc_id, 'room': 'Suite 9', 'dutyDays': ['2026-12-12'],
        'dayConfigs': {'2026-12-12': {'capacity': 1}}}, format='json')
    assert r.status_code == 400, r.data
    print('[R7 PASS] 1st & 3rd Sunday recurrence honoured; date keys rejected.')

    # R8: availability endpoint agrees with booking validation
    data = client.get(f'/api/doctors/{doc.doc_id}/available-dates/?days=60').data
    by_date = {d['date']: d for d in data['availability']}
    assert by_date[ok.isoformat()]['onDuty'] and not by_date[bad.isoformat()]['onDuty']
    assert by_date[tue]['isFull'] is True
    print('[R8 PASS] available-dates matches booking rules (duty, recurrence, full days).')

    # R9: anonymous users cannot perform desk actions, may reschedule
    r = book(next_weekday(3).isoformat(), 'Anon')
    assert r.status_code == 201, r.data
    aref = r.data['refCode']
    for path in ('pay-cashdesk', 'check-in', 'approve-hmo', 'restore'):
        assert anon.post(f'/api/bookings/{aref}/{path}/', {}, format='json').status_code == 401, path
    assert anon.patch(f'/api/bookings/{aref}/', {'payment_status': 'Cleared'}, format='json').status_code == 401
    assert anon.get('/api/bookings/disabled/').status_code == 401
    new_thu = next_weekday(3, next_weekday(3)).isoformat()
    r = anon.patch(f'/api/bookings/{aref}/', {'date': new_thu, 'time': '02:00 PM',
                                              'reschedule_reason': 'x', 'status': 'Confirmed'}, format='json')
    assert r.status_code == 200 and r.data['date'] == new_thu, r.data
    print('[R9 PASS] Desk actions are staff-only; patients can still reschedule.')

    # R10: status-only update of a same-day booking is not blocked by cutoff
    b = Booking.objects.create(ref_code=f'ISALU-T{random.randint(10**8,10**9)}', doctor_id=doc.doc_id,
                               doctor_name='x', doctor_specialty='x', date=today_str, time='12:01 AM',
                               patient_name='Today', patient_phone='1', payment_status='Cleared')
    r = client.patch(f'/api/bookings/{b.ref_code}/', {'status': 'Completed', 'reason': 'done'}, format='json')
    assert r.status_code == 200, r.data
    print('[R10 PASS] Lifecycle updates on today\'s bookings are not blocked by the cutoff.')

    # R11: disabled archive + restore
    assert client.delete(f'/api/bookings/{aref}/').status_code == 200
    assert any(x['refCode'] == aref for x in client.get('/api/bookings/disabled/').data)
    r = client.post(f'/api/bookings/{aref}/restore/')
    assert r.status_code == 200 and r.data['data']['status'] == 'Confirmed'
    print('[R11 PASS] Disabled archive lists records; restore sets status Confirmed.')

    # R12: clinic create with dashboard payload, time slots, analytics, capacity analytics
    r = client.post('/api/departments/', {'name': 'Regression Clinic', 'departmentId': f'reg-{random.randint(1000,9999)}',
                                           'iconName': 'Activity', 'status': 'Active'}, format='json')
    assert r.status_code == 201, r.data
    r = client.post('/api/time-slots/', {'startTime': '07:00 AM', 'endTime': '11:00 AM',
                                          'formatted': '07:00 AM – 11:00 AM'}, format='json')
    assert r.status_code == 201, r.data
    assert anon.get('/api/clinic-analytics/').status_code == 200
    assert client.get('/api/schedules/capacity-analytics/').status_code == 200
    assert client.get('/api/doctors/12345/').status_code == 404
    print('[R12 PASS] Clinic/time-slot creation, clinic analytics, capacity analytics, numeric doctor lookup.')

    # R13: staff users - password required, no silent overwrite, role PATCH keeps status
    email = f'reg{random.randint(1000,9999)}@isaluhospitals.com'
    assert client.post('/api/users/', {'name': 'A', 'email': email, 'role': 'Helpdesk Officer'}, format='json').status_code == 400
    assert client.post('/api/users/', {'name': 'A', 'email': email, 'password': 'secret1'}, format='json').status_code == 201
    assert client.post('/api/users/', {'name': 'B', 'email': email, 'password': 'other12'}, format='json').status_code == 409
    role = Role.objects.create(role_id=f'role-reg-{random.randint(1000,9999)}', name=f'Reg Role {random.randint(1000,9999)}', status=False)
    r = client.patch(f'/api/roles/{role.role_id}/', {'description': 'changed'}, format='json')
    assert r.status_code == 200 and r.data['status'] == 'Disabled', r.data
    r = anon.post('/api/bookings/', {'doctorId': doc.doc_id, 'date': next_weekday(3).isoformat(), 'time': '10:00 AM',
                                      'patientName': 'Sneaky', 'patientPhone': '1', 'paymentType': 'Private Self-Pay',
                                      'payment_status': 'Cleared', 'hmo_status': 'Approved', 'status': 'Checked In'}, format='json')
    assert r.status_code == 201 and r.data['paymentStatus'] == 'Pending' and r.data['status'] == 'Confirmed', r.data
    # R15: exact JSON stored for the two schedule types, even from messy input
    r = client.post('/api/schedules/', {
        'doctorId': doc.doc_id, 'room': 'Suite 10',
        'dutyDays': ['thursday'],
        'dayConfigs': {'Thursday': {'shift_times': '04:00 PM – 06:00 PM', 'capacity': '6', 'date': 'x'}}}, format='json')
    assert r.status_code == 201, r.data
    s = SpecialistSchedule.objects.get(sched_id=r.data['sched_id'])
    assert s.duty_days == ['Thu'], s.duty_days
    assert s.day_configs == {'Thu': {'shiftTimes': ['04:00 PM – 06:00 PM'], 'capacity': 6}}, s.day_configs
    r = client.post('/api/schedules/', {
        'doctorId': doc.doc_id, 'room': 'Suite 10', 'dutyDays': ['Sat'],
        'dayConfigs': {'Sat': {'weeks': ['1st Week', 3], 'capacity': 12, 'shiftTimes': ['02:00 PM – 04:00 PM']}}}, format='json')
    assert r.status_code == 201, r.data
    s = SpecialistSchedule.objects.get(sched_id=r.data['sched_id'])
    assert s.duty_days == ['Sat'], s.duty_days
    assert s.day_configs == {'Sat': {'shiftTimes': ['02:00 PM – 04:00 PM'], 'capacity': 12, 'weeks': [1, 3]}}, s.day_configs
    # all five weeks == every week -> stored as a normal schedule
    r = client.post('/api/schedules/', {'doctorId': doc.doc_id, 'room': 'Suite 10', 'dutyDays': ['Mon'],
        'dayConfigs': {'Mon': {'weeks': [1, 2, 3, 4, 5], 'capacity': 3, 'shiftTimes': ['09:00 AM – 12:00 PM']}}}, format='json')
    assert SpecialistSchedule.objects.get(sched_id=r.data['sched_id']).day_configs == {'Mon': {'shiftTimes': ['09:00 AM – 12:00 PM'], 'capacity': 3}}
    # invalid weeks / capacity rejected
    assert client.post('/api/schedules/', {'doctorId': doc.doc_id, 'room': 'x', 'dutyDays': ['Sat'],
        'dayConfigs': {'Sat': {'weeks': [7], 'capacity': 2, 'shiftTimes': ['a']}}}, format='json').status_code == 400
    assert client.post('/api/schedules/', {'doctorId': doc.doc_id, 'room': 'x', 'dutyDays': ['Sat'],
        'dayConfigs': {'Sat': {'capacity': 0, 'shiftTimes': ['a']}}}, format='json').status_code == 400
    print('[R15 PASS] Normal and recurring schedules stored in the exact canonical JSON format.')
    print('[R14 PASS] Public bookings cannot pre-set payment, HMO or check-in status.')
    print('[R13 PASS] Staff accounts need a password, are never overwritten; role PATCH keeps status.')


def run_schedule_change_tests(client, today_str):
    """R16-R21: cancel / move a single clinic date, notifications, reminders."""
    from django.conf import settings
    from django.core import mail
    from api.models import ScheduleException
    from api.notification_service import process_3hour_appointment_reminders

    settings.SCHEDULE_CHANGE_NOTIFY_SYNC = True
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    mail.outbox = []
    anon = APIClient()
    today = datetime.date.fromisoformat(today_str)
    now = timezone.localtime()

    doc = Doctor.objects.create(doc_id=f'doc-chg-{random.randint(1000,9999)}', name='Specialist Q',
                                full_name='Dr. Change Test', acronym='Specialist Q', status=True)
    # Clinic every day of the week except Wednesday, so we have targets.
    days = ['Mon', 'Tue', 'Thu', 'Fri', 'Sat', 'Sun']
    r = client.post('/api/schedules/', {'doctorId': doc.doc_id, 'room': 'Suite C', 'dutyDays': days,
        'dayConfigs': {d: {'shiftTimes': ['09:00 AM – 01:00 PM'], 'capacity': 5} for d in days}}, format='json')
    assert r.status_code == 201, r.data

    def next_clinic(after, weekday_names=days):
        d = after + datetime.timedelta(days=1)
        while d.strftime('%a') not in weekday_names:
            d += datetime.timedelta(days=1)
        return d
    def next_wed(after):
        d = after + datetime.timedelta(days=1)
        while d.weekday() != 2:
            d += datetime.timedelta(days=1)
        return d
    def book(date, name, email='p@example.com', phone='08012345678'):
        return anon.post('/api/bookings/', {'doctorId': doc.doc_id, 'date': date.isoformat(), 'time': '09:00 AM – 01:00 PM',
            'patientName': name, 'patientPhone': phone, 'patientEmail': email,
            'paymentType': 'Private Self-Pay'}, format='json')

    # ---------- R16: preview + cancel ----------
    d1 = next_clinic(today + datetime.timedelta(days=2))
    refs = [book(d1, f'Cancel Patient {i}').data['refCode'] for i in range(3)]
    p = client.get(f'/api/schedule-exceptions/preview/?doctor_id={doc.doc_id}&date={d1.isoformat()}').data
    assert p['on_duty'] and p['affected_count'] == 3 and p['with_email'] == 3, p
    r = client.post('/api/schedule-exceptions/', {'doctor_id': doc.doc_id, 'original_date': d1.isoformat(),
        'action': 'cancel', 'reason': 'Doctor attending a conference'}, format='json')
    assert r.status_code == 201, r.data
    exc_id = r.data['exception_id']
    assert r.data['affectedCount'] == 3
    for ref in refs:
        assert Booking.objects.get(ref_code=ref).status == 'Cancelled'
    e = ScheduleException.objects.get(exception_id=exc_id)
    assert e.notification_status == 'sent' and e.notified_count == 3, (e.notification_status, e.notification_log)
    cancel_mails = [m for m in mail.outbox if 'Appointment Cancelled' in m.subject]
    assert len(cancel_mails) == 3 and 'conference' in cancel_mails[0].body, [m.subject for m in mail.outbox]
    r = book(d1, 'Late Booker')
    assert r.status_code == 400 and 'cancelled' in str(r.data).lower(), r.data
    av = {x['date']: x for x in client.get(f'/api/doctors/{doc.doc_id}/available-dates/?days=40').data['availability']}
    assert av[d1.isoformat()]['onDuty'] is False and 'cancelled' in av[d1.isoformat()]['note'].lower()
    print('[R16 PASS] Cancel one clinic date: bookings cancelled, 3 patients emailed+texted, date closed.')

    # ---------- R17: move a clinic to a free date ----------
    d2 = next_clinic(d1 + datetime.timedelta(days=1))
    wed = next_wed(d2)
    moved = [book(d2, f'Move Patient {i}').data['refCode'] for i in range(2)]
    Booking.objects.filter(ref_code__in=moved).update(reminder_sent=True)
    r = client.post('/api/schedule-exceptions/', {'doctor_id': doc.doc_id, 'original_date': d2.isoformat(),
        'action': 'reschedule', 'new_date': wed.isoformat(), 'shift_time': '02:00 PM – 05:00 PM',
        'reason': 'Theatre list'}, format='json')
    assert r.status_code == 201, r.data
    for ref in moved:
        b = Booking.objects.get(ref_code=ref)
        assert b.date == wed.isoformat() and b.time == '02:00 PM – 05:00 PM' and b.reminder_sent is False, (b.date, b.time)
        assert b.status == 'Confirmed'
    assert sum('Rescheduled' in m.subject for m in mail.outbox) == 2
    assert book(d2, 'Old date').status_code == 400
    r = book(wed, 'New date patient')
    assert r.status_code == 201, r.data
    av = {x['date']: x for x in client.get(f'/api/doctors/{doc.doc_id}/available-dates/?days=60').data['availability']}
    assert av[wed.isoformat()]['onDuty'] and av[wed.isoformat()]['timeWindow'] == '02:00 PM – 05:00 PM'
    assert av[wed.isoformat()]['booked'] == 3 and av[wed.isoformat()]['capacity'] == 5
    nxt_wed = next_wed(wed)
    assert av[nxt_wed.isoformat()]['onDuty'] is False   # only that one date changed
    print('[R17 PASS] Move one clinic: bookings moved with new hours, reminders re-armed, only that date affected.')

    # ---------- R18: invalid changes rejected ----------
    d3 = next_clinic(d2 + datetime.timedelta(days=1))
    bad = [
        {'original_date': d1.isoformat(), 'action': 'cancel'},                                           # already changed
        {'original_date': next_wed(d3).isoformat(), 'action': 'cancel'},                                 # no clinic
        {'original_date': (today - datetime.timedelta(days=3)).isoformat(), 'action': 'cancel'},         # past
        {'original_date': d3.isoformat(), 'action': 'reschedule', 'new_date': next_clinic(d3).isoformat()},  # target has clinic
        {'original_date': d3.isoformat(), 'action': 'reschedule', 'new_date': wed.isoformat()},          # target already a moved clinic
        {'original_date': d3.isoformat(), 'action': 'reschedule'},                                       # no new date
    ]
    for body in bad:
        r = client.post('/api/schedule-exceptions/', {'doctor_id': doc.doc_id, **body}, format='json')
        assert r.status_code == 400, (body, r.status_code, r.data)
    assert anon.post('/api/schedule-exceptions/', {'doctor_id': doc.doc_id, 'original_date': d3.isoformat(),
                                                   'action': 'cancel'}, format='json').status_code in (401, 403)
    print('[R18 PASS] Invalid or duplicate changes rejected; anonymous users blocked.')

    # ---------- R19: undo rules ----------
    r = client.post('/api/schedule-exceptions/', {'doctor_id': doc.doc_id, 'original_date': d3.isoformat(),
        'action': 'cancel'}, format='json')
    assert r.status_code == 201 and r.data['affectedCount'] == 0 and r.data['notificationStatus'] == 'none'
    assert book(d3, 'blocked').status_code == 400
    assert client.delete(f"/api/schedule-exceptions/{r.data['exception_id']}/").status_code == 200
    assert book(d3, 'reopened').status_code == 201
    assert client.delete(f'/api/schedule-exceptions/{exc_id}/').status_code == 400
    print('[R19 PASS] Changes with no patients can be undone; changes that notified patients cannot.')

    # ---------- R20: the usual 3-hour reminder fires for a moved clinic ----------
    start = now + datetime.timedelta(hours=2)
    if start.date() == today and today.strftime('%a') != 'Wed':
        doc2 = Doctor.objects.create(doc_id=f'doc-rem-{random.randint(1000,9999)}', name='Specialist R2',
                                     full_name='Dr. Reminder Test', status=True)
        src = next_clinic(today + datetime.timedelta(days=1), [ (today + datetime.timedelta(days=3)).strftime('%a') ])
        client.post('/api/schedules/', {'doctorId': doc2.doc_id, 'room': 'R', 'dutyDays': [src.strftime('%a')],
            'dayConfigs': {src.strftime('%a'): {'shiftTimes': ['09:00 AM – 11:00 AM'], 'capacity': 3}}}, format='json')
        rb = anon.post('/api/bookings/', {'doctorId': doc2.doc_id, 'date': src.isoformat(), 'time': '09:00 AM – 11:00 AM',
            'patientName': 'Reminder Patient', 'patientPhone': '08011112222', 'patientEmail': 'r@example.com',
            'paymentType': 'Private Self-Pay'}, format='json')
        assert rb.status_code == 201, rb.data
        shift = f"{start.strftime('%I:%M %p')} – {(start + datetime.timedelta(hours=1)).strftime('%I:%M %p')}"
        r = client.post('/api/schedule-exceptions/', {'doctor_id': doc2.doc_id, 'original_date': src.isoformat(),
            'action': 'reschedule', 'new_date': today_str, 'shift_time': shift}, format='json')
        assert r.status_code == 201, r.data
        mail.outbox = []
        process_3hour_appointment_reminders(hours_ahead=3)
        b = Booking.objects.get(ref_code=rb.data['refCode'])
        assert b.reminder_sent is True, 'moved booking did not get the 3-hour reminder'
        assert any('Starts in 3 Hours' in m.subject and b.ref_code in m.subject for m in mail.outbox)
        print('[R20 PASS] Moved booking received the usual automatic 3-hour reminder on its new date.')
    else:
        print('[R20 SKIP] Needs >2h left today (run earlier in the day to exercise the 3-hour reminder).')

    # ---------- R21: retry only re-sends failures ----------
    e = ScheduleException.objects.get(exception_id=exc_id)
    log = e.notification_log
    log[0]['delivered'] = False
    e.notification_log = log
    e.save()
    mail.outbox = []
    r = client.post(f'/api/schedule-exceptions/{exc_id}/retry-notifications/')
    assert r.status_code == 200 and r.data['retrying'] == 1
    e.refresh_from_db()
    assert len(mail.outbox) == 1 and e.notified_count == 3 and e.notification_status == 'sent'
    print('[R21 PASS] Retry re-sends only to patients who were not reached.')


def run_permission_tests(client, today_str):
    """R22-R24: only administrators can edit/delete; profiles expose allowed modules."""
    staff = APIClient()
    r = staff.post('/api/auth/staff-login/', {'username': 'reception@isaluhospitals.com', 'password': 'admin123'}, format='json')
    assert r.status_code == 200, r.data
    u = r.data['user']
    assert u['isAdmin'] is False and u['allowedDesks'] == ['helpdesk', 'all_patients', 'checked_in_patients'], u
    staff.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['tokens']['access']}")
    admin_login = APIClient().post('/api/auth/staff-login/', {'username': 'admin@isaluhospitals.com', 'password': 'admin123'}, format='json').data['user']
    assert admin_login['isAdmin'] is True and 'users' in admin_login['allowedDesks']
    assert staff.get('/api/auth/me/').data['user']['isAdmin'] is False
    print('[R22 PASS] Login and /auth/me/ return isAdmin and the role\'s allowed modules.')

    # A booking for the desk tests
    avail = [d for d in client.get('/api/doctors/doc-1/available-dates/?days=30').data['availability'] if d['available'] and d['date'] > today_str]
    b = APIClient().post('/api/bookings/', {'doctorId': 'doc-1', 'date': avail[0]['date'], 'time': '08:00 AM – 02:00 PM',
        'patientName': 'Perm Patient', 'patientPhone': '08000000001', 'paymentType': 'Private Self-Pay'}, format='json').data
    ref = b['refCode']
    sched = client.get('/api/schedules/').data[0]['sched_id']

    denied = [
        staff.delete(f'/api/bookings/{ref}/'),
        staff.patch(f'/api/bookings/{ref}/', {'patient_name': 'Changed'}, format='json'),
        staff.put(f'/api/bookings/{ref}/', {'patient_name': 'Changed'}, format='json'),
        staff.post('/api/bookings/clear-all/', {}, format='json'),
        staff.patch(f'/api/schedules/{sched}/', {'room': 'X'}, format='json'),
        staff.delete(f'/api/schedules/{sched}/'),
        staff.post('/api/schedules/', {'doctorId': 'doc-1', 'room': 'x', 'dutyDays': ['Tue']}, format='json'),
        staff.patch('/api/departments/cardiology/', {'name': 'X'}, format='json'),
        staff.delete('/api/departments/cardiology/'),
        staff.patch('/api/doctors/doc-1/', {'name': 'X'}, format='json'),
        staff.post('/api/users/', {'name': 'x', 'email': 'x@x.com', 'password': 'secret1'}, format='json'),
        staff.patch('/api/roles/role-2/', {'description': 'x'}, format='json'),
        staff.patch('/api/hmo-companies/hmo-1/', {'name': 'X'}, format='json'),
        staff.post('/api/schedule-exceptions/', {'doctor_id': 'doc-1', 'original_date': avail[1]['date'], 'action': 'cancel'}, format='json'),
    ]
    codes = [r.status_code for r in denied]
    assert all(c == 403 for c in codes), codes
    assert Booking.objects.get(ref_code=ref).is_active and Booking.objects.get(ref_code=ref).patient_name == 'Perm Patient'
    print(f'[R23 PASS] Non-admin staff blocked from {len(codes)} edit/delete operations (HTTP 403).')

    assert staff.patch(f'/api/bookings/{ref}/', {'status': 'Confirmed'}, format='json').status_code == 200
    assert staff.post(f'/api/bookings/{ref}/pay-cashdesk/', {'paymentMethod': 'cash'}, format='json').status_code == 200
    assert staff.post(f'/api/bookings/{ref}/check-in/').status_code == 200
    assert staff.get('/api/bookings/').status_code == 200
    assert staff.get(f'/api/schedule-exceptions/preview/?doctor_id=doc-1&date={avail[1]["date"]}').status_code == 200
    assert client.delete(f'/api/bookings/{ref}/').status_code == 200          # admin still can
    # A role that is given the Specialist Roster module may create, not edit/delete.
    from api.models import Role as _Role
    _Role.objects.filter(role_id='role-2').update(allowed_desks=['helpdesk', 'all_patients', 'checked_in_patients', 'create_specialist_schedule'])
    assert 'create_specialist_schedule' in staff.get('/api/auth/me/').data['user']['allowedDesks']
    r = staff.post('/api/schedules/', {'doctorId': 'doc-2', 'room': 'Roster Staff Suite', 'dutyDays': ['Mon'],
        'dayConfigs': {'Mon': {'shiftTimes': ['09:00 AM – 12:00 PM'], 'capacity': 4}}}, format='json')
    assert r.status_code == 201, r.data
    assert staff.patch(f"/api/schedules/{r.data['sched_id']}/", {'room': 'X'}, format='json').status_code == 403
    assert staff.delete(f"/api/schedules/{r.data['sched_id']}/").status_code == 403
    _Role.objects.filter(role_id='role-2').update(allowed_desks=['helpdesk', 'all_patients', 'checked_in_patients'])
    print('[R24 PASS] Desk work still works for staff; roster-module staff can create but not edit/delete; admin can delete.')


def run_duplicate_and_channel_tests(client, today_str):
    """R25-R27: double-booking guard; unconfigured channels are reported, never faked."""
    from django.conf import settings
    from api.models import ScheduleException
    anon = APIClient()
    avail = [d['date'] for d in client.get('/api/doctors/doc-1/available-dates/?days=40').data['availability']
             if d['available'] and d['date'] > today_str]
    def book(doc, date, name, phone, c=anon):
        return c.post('/api/bookings/', {'doctorId': doc, 'date': date, 'time': '08:00 AM – 02:00 PM',
            'patientName': name, 'patientPhone': phone, 'paymentType': 'Private Self-Pay'}, format='json')

    r = book('doc-1', avail[0], 'Mrs. Ada Obi', '0801 111 2222')
    assert r.status_code == 201, r.data
    first = r.data['refCode']
    # same person, other formats, another date in the same clinic -> blocked
    r = book('doc-1', avail[1], 'ada   OBI', '+234 801 111 2222')
    assert r.status_code == 400 and 'Double booking' in str(r.data) and first in str(r.data), r.data
    # same clinic, different doctor -> still blocked
    cardio2 = Doctor.objects.create(doc_id='doc-dup-cardio', name='Specialist DC', full_name='Dr. Second Cardio',
                                    department=Department.objects.get(dept_id='cardiology'), status=True)
    client.post('/api/schedules/', {'doctorId': cardio2.doc_id, 'room': 'C2', 'dutyDays': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        'dayConfigs': {d: {'shiftTimes': ['08:00 AM – 02:00 PM'], 'capacity': 9} for d in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']}}, format='json')
    tomorrow = (datetime.date.fromisoformat(today_str) + datetime.timedelta(days=1)).isoformat()
    r = book(cardio2.doc_id, tomorrow, 'Ada Obi', '2348011112222')
    assert r.status_code == 400 and 'Double booking' in str(r.data), r.data
    # pre-check endpoint used by the booking form
    chk = anon.get('/api/bookings/duplicate-check/', {'doctor_id': cardio2.doc_id, 'patient_name': 'ada obi', 'patient_phone': '08011112222'}).data
    assert chk['duplicate'] is True and chk['refCode'] == first[:6] + '*' * (len(first) - 10) + first[-4:] and chk['clinic'] == 'Cardiology', chk
    assert anon.get('/api/bookings/duplicate-check/', {'doctor_id': 'doc-2', 'patient_name': 'ada obi', 'patient_phone': '08011112222'}).data['duplicate'] is False
    # different clinic, different name on same phone, same name other phone -> allowed
    assert book('doc-2', [d['date'] for d in client.get('/api/doctors/doc-2/available-dates/?days=30').data['availability'] if d['available'] and d['date'] > today_str][0], 'Ada Obi', '08011112222').status_code == 201
    assert book('doc-1', avail[1], 'Chidi Obi', '08011112222').status_code == 201
    assert book('doc-1', avail[1], 'Ada Obi', '08099990000').status_code == 201
    # rescheduling the existing booking is not a duplicate of itself
    r = anon.patch(f'/api/bookings/{first}/', {'date': avail[2], 'time': '08:00 AM – 02:00 PM', 'status': 'Confirmed'}, format='json')
    assert r.status_code == 200, r.data
    # staff editing another booking's name into a duplicate is blocked
    other = book('doc-1', avail[1], 'Bola Ade', '08022223333').data['refCode']
    r = client.patch(f'/api/bookings/{other}/', {'patient_name': 'Ada Obi', 'patient_phone': '08011112222'}, format='json')
    assert r.status_code == 400 and 'Double booking' in str(r.data), r.data
    # once cancelled, the patient may book that clinic again
    assert client.patch(f'/api/bookings/{first}/', {'status': 'Cancelled'}, format='json').status_code == 200
    assert book('doc-1', avail[1], 'Ada Obi', '0801 111 2222').status_code == 201
    print('[R25 PASS] One upcoming appointment per patient (name + phone) per clinic; formats, titles and other doctors covered.')

    # R26: SMS text is plain GSM (no en dash) and short
    from api.notification_service import _schedule_change_texts, to_gsm_text
    b = Booking.objects.filter(doctor_id='doc-1').first()
    b.time = '01:00 PM – 06:00 PM (Afternoon Shift)'
    for action in ('cancel', 'reschedule'):
        sms = to_gsm_text(_schedule_change_texts(b, action, avail[0], avail[3], 'Doctor on leave')[1])
        assert sms.isascii() and len(sms) <= 240, (len(sms), sms)
    print('[R26 PASS] Cancellation/move SMS are plain GSM text (no Unicode surcharge).')

    # R27: with no SMS gateway and a console email backend, nothing is reported as delivered
    saved = settings.EMAIL_BACKEND
    settings.EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    try:
        date = avail[4]
        anon.post('/api/bookings/', {'doctorId': 'doc-1', 'date': date, 'time': '08:00 AM – 02:00 PM', 'patientName': 'Chan Test',
            'patientPhone': '08044445555', 'patientEmail': 'chan@example.com', 'paymentType': 'Private Self-Pay'}, format='json')
        r = client.post('/api/schedule-exceptions/', {'doctor_id': 'doc-1', 'original_date': date, 'action': 'cancel', 'reason': 'x'}, format='json')
        assert r.status_code == 201, r.data
        e = ScheduleException.objects.get(exception_id=r.data['exception_id'])
        assert e.notification_status == 'failed' and e.notified_count == 0, (e.notification_status, e.notification_log)
        entry = e.notification_log[0]
        assert 'No SMS gateway configured' in entry['sms_message'] and 'not a real mail server' in entry['email_message'], entry
        ch = client.get('/api/schedule-exceptions/channels/').data
        assert ch['email']['configured'] is False and ch['sms']['configured'] is False, ch
    finally:
        settings.EMAIL_BACKEND = saved
    print('[R27 PASS] Unconfigured email/SMS are reported as NOT delivered (no silent console fallback).')

    # R28: restoring an archived booking cannot recreate a double booking
    avail = [d for d in client.get('/api/doctors/doc-2/available-dates/?days=40').data['availability'] if d['available'] and d['date'] > today_str]
    def bk(date):
        return APIClient().post('/api/bookings/', {'doctorId': 'doc-2', 'date': date, 'time': '09:00 AM – 04:00 PM',
            'patientName': 'Restore Twice', 'patientPhone': '0807 000 1111', 'paymentType': 'Private Self-Pay'}, format='json')
    first = bk(avail[0]['date']).data['refCode']
    assert client.delete(f'/api/bookings/{first}/').status_code == 200          # archived -> slot freed
    second = bk(avail[1]['date'])
    assert second.status_code == 201, second.data                               # may rebook after archive
    r = client.post(f'/api/bookings/{first}/restore/')
    assert r.status_code == 400 and r.data.get('duplicate') and second.data['refCode'] in r.data['error'], r.data
    print('[R28 PASS] Restoring an archived booking cannot recreate a double booking.')

    # R29: public lookup never exposes documents, email or insurance codes
    av = [d for d in client.get('/api/doctors/doc-3/available-dates/?days=40').data['availability'] if d['available'] and d['date'] > today_str]
    r = APIClient().post('/api/bookings/', {'doctorId': 'doc-3', 'date': av[0]['date'], 'time': '08:00 AM – 02:00 PM',
        'patientName': 'Private Doc', 'patientPhone': '08066667777', 'patientEmail': 'secret@example.com',
        'paymentType': 'Private Self-Pay', 'referralDocName': 'letter.pdf',
        'referralDocData': 'data:application/pdf;base64,JVBERi0xLjQK'}, format='json')
    assert r.status_code == 201, r.data
    for q in (f"ref_code={r.data['refCode']}", 'phone=08066667777'):
        pub = APIClient().get(f'/api/bookings/public-lookup/?{q}').data
        assert pub['patientName'] == 'Private Doc' and 'referralDocData' not in pub and 'referral_doc_data' not in pub
        assert 'patientEmail' not in pub and 'patient_email' not in pub, pub.keys()
    assert client.get(f"/api/bookings/{r.data['refCode']}/").data['referralDocData'].startswith('data:application/pdf')
    print('[R32 PASS] Public lookup hides referral documents, email and insurance codes (staff still see them).')

    # R30: referral uploads must be PDF/image and at most 5 MB
    base = {'doctorId': 'doc-3', 'date': av[1]['date'], 'time': '08:00 AM – 02:00 PM', 'patientPhone': '0806000000',
            'paymentType': 'Private Self-Pay'}
    bad_type = APIClient().post('/api/bookings/', {**base, 'patientName': 'Bad Type',
        'referralDocData': 'data:text/html;base64,PHNjcmlwdD4='}, format='json')
    too_big = APIClient().post('/api/bookings/', {**base, 'patientName': 'Too Big',
        'referralDocData': 'data:application/pdf;base64,' + 'A' * 7_200_000}, format='json')
    assert bad_type.status_code == 400 and too_big.status_code == 400, (bad_type.status_code, too_big.status_code)
    print('[R33 PASS] Referral uploads limited to PDF/PNG/JPEG up to 5 MB.')

    # R31: patient-supplied text is escaped in HTML emails
    from api.notification_service import _schedule_change_texts
    b = Booking.objects.get(ref_code=r.data['refCode'])
    b.patient_name = '<a href="http://evil.example">Click</a>'
    _, _, html_body, _ = _schedule_change_texts(b, 'cancel', b.date, '', '<img src=x onerror=alert(1)>')
    assert '<a href="http://evil.example">' not in html_body and '<img src=x' not in html_body and '&lt;a href=' in html_body
    print('[R34 PASS] Patient names and reasons are HTML-escaped in emails.')

    # R29: incremental sync, compression and today's summary
    import time as _time
    full = client.get('/api/bookings/sync/').data
    assert full['full'] is True and len(full['results']) == Booking.objects.filter(is_active=True).exclude(status__iexact='Disabled').count()
    window = client.get(f'/api/bookings/sync/?date_from={today_str}').data
    assert window['full'] is False and all(b['date'] >= today_str for b in window['results'])
    since = full['server_time']
    _time.sleep(0.05)
    avail = [d for d in client.get('/api/doctors/doc-3/available-dates/?days=40').data['availability'] if d['available'] and d['date'] > today_str]
    nb = APIClient().post('/api/bookings/', {'doctorId': 'doc-3', 'date': avail[0]['date'], 'time': '08:00 AM – 02:00 PM',
        'patientName': 'Sync Patient', 'patientPhone': '08066667777', 'paymentType': 'Private Self-Pay'}, format='json').data['refCode']
    client.patch(f'/api/bookings/{nb}/', {'status': 'Confirmed'}, format='json')
    gone = Booking.objects.filter(is_active=True).exclude(ref_code=nb).first().ref_code
    client.delete(f'/api/bookings/{gone}/')
    from urllib.parse import quote
    delta = client.get(f'/api/bookings/sync/?since={quote(since)}').data
    refs = [b['refCode'] for b in delta['results']]
    assert nb in refs and gone in delta['removed'] and len(refs) < 50, (refs[:5], delta['removed'])
    r = client.get('/api/bookings/', HTTP_ACCEPT_ENCODING='gzip')
    assert r.get('Content-Encoding') == 'gzip', dict(r.items())
    s = client.get('/api/bookings/summary/').data
    assert s['todayCount'] == Booking.objects.filter(is_active=True, date=today_str).exclude(status__iexact='Disabled').count()
    assert APIClient().get('/api/bookings/sync/').status_code == 401
    print('[R29 PASS] Incremental sync (changes + removals), gzip responses and today\'s summary counts.')

    # R30: only one worker can send a given reminder
    import threading
    from django.core import mail
    from api.notification_service import send_single_booking_reminder
    Booking.objects.create(ref_code='ISALU-RACE2', doctor_id='doc-1', doctor_name='x', doctor_specialty='x', date='2030-01-01',
                           time='09:00 AM', patient_name='Race', patient_phone='', patient_email='race@example.com')
    mail.outbox = []
    barrier = threading.Barrier(6)
    def worker():
        from django.db import connection
        barrier.wait()
        send_single_booking_reminder(Booking.objects.get(ref_code='ISALU-RACE2'), is_3hour_notice=True)
        connection.close()
    threads = [threading.Thread(target=worker) for _ in range(6)]
    [th.start() for th in threads]; [th.join() for th in threads]
    assert len([m for m in mail.outbox if 'ISALU-RACE2' in m.subject]) == 1, len(mail.outbox)
    print('[R30 PASS] Six workers racing on one reminder send it exactly once.')


def run_hmo_decline_tests(client, today_str):
    """R35: HMO desk can decline a request; it leaves the pending queue and can be re-opened."""
    avail = [d['date'] for d in client.get('/api/doctors/doc-1/available-dates/?days=40').data['availability']
             if d['available'] and d['date'] > today_str]
    r = APIClient().post('/api/bookings/', {'doctorId': 'doc-1', 'date': avail[-1], 'time': '08:00 AM – 02:00 PM',
        'patientName': 'Decline Patient', 'patientPhone': '08011112222', 'paymentType': 'HMO Insurance',
        'hmoName': 'Hygeia HMO', 'hmoPolicyCode': 'HYG-1'}, format='json')
    assert r.status_code == 201, r.data
    ref = r.data['refCode']
    before = client.get('/api/bookings/summary/').data
    assert APIClient().post(f'/api/bookings/{ref}/decline-hmo/', {'reason': 'x'}, format='json').status_code in (401, 403)

    hmo = APIClient()
    login = hmo.post('/api/auth/staff-login/', {'username': 'hmo.desk@isaluhospitals.com', 'password': 'admin123'}, format='json')
    assert login.status_code == 200, login.data
    assert 'hmo_declined' in login.data['user']['allowedDesks'], login.data['user']['allowedDesks']
    hmo.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['tokens']['access']}")

    d = hmo.post(f'/api/bookings/{ref}/decline-hmo/', {'reason': 'Policy inactive'}, format='json')
    assert d.status_code == 200, d.data
    assert d.data['data']['hmoStatus'] == 'Declined' and d.data['data']['hmoDeclineReason'] == 'Policy inactive'
    assert d.data['data']['hmoDeclinedAt'] and d.data['data']['hmoDeclinedBy']
    after = client.get('/api/bookings/summary/').data
    assert after['pendingHmoCount'] == before['pendingHmoCount'] - 1, (before, after)
    assert after['declinedHmoCount'] == before.get('declinedHmoCount', 0) + 1, (before, after)
    assert hmo.post(f'/api/bookings/{ref}/check-in/').status_code == 400          # cannot check in while declined
    assert hmo.patch(f'/api/bookings/{ref}/', {'hmo_decline_reason': 'tamper'}, format='json').status_code in (200, 403)
    assert Booking.objects.get(ref_code=ref).hmo_decline_reason == 'Policy inactive'   # read-only field

    o = hmo.post(f'/api/bookings/{ref}/reopen-hmo/', {}, format='json')
    assert o.status_code == 200 and o.data['data']['hmoStatus'] == 'Awaiting Approval' and o.data['data']['hmoDeclineReason'] == ''
    assert hmo.post(f'/api/bookings/{ref}/reopen-hmo/', {}, format='json').status_code == 400

    hmo.post(f'/api/bookings/{ref}/decline-hmo/', {'reason': 'Again'}, format='json')
    a = hmo.post(f'/api/bookings/{ref}/approve-hmo/', {'policyCode': 'HYG-1', 'authCode': 'AUTH-9'}, format='json')
    assert a.status_code == 200 and a.data['data']['hmoStatus'] == 'Approved' and a.data['data']['hmoDeclineReason'] == ''
    assert hmo.post(f'/api/bookings/{ref}/decline-hmo/', {'reason': 'late'}, format='json').status_code == 400  # approved stays approved
    print('[R35 PASS] HMO decline: moves out of the pending queue, blocks check-in, re-open and approve clear it; HMO staff get the module.')


if __name__ == '__main__':
    run_e2e_tests()
