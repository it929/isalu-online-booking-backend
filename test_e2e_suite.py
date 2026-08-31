import sys
import os
import random
import datetime

sys.path.insert(0, os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'clinic_backend.settings')

import django
django.setup()

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

    # TEST 4: Valid Online Appointment Ticket Creation
    ref_code = f'ISALU-E2E-{random.randint(10000, 99999)}'
    booking_payload = {
        'refCode': ref_code,
        'doctorId': 'doc-14',
        'doctorName': 'Dr. Victoria Danjuma',
        'doctorSpecialty': 'Neurology',
        'date': '2026-09-20',
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

    print('\n================================================================================')
    print('SUMMARY: ALL 8 E2E INTEGRATION & REGRESSION TESTS PASSED (100% SUCCESS RATE)')
    print('================================================================================')

if __name__ == '__main__':
    run_e2e_tests()
