import os
import django
import random
import datetime
from django.utils import timezone

# REPLACE 'edtekapp.settings' WITH YOUR ACTUAL SETTINGS MODULE 
# (e.g., 'core.settings', 'config.settings', or 'clinic_booking.settings')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'clinic_backend.settings')
django.setup()

from api.models import Booking, Doctor

def generate_bookings():
    print("🚀 Starting generation of 3,000 mock booking records...")
    
    doctors = list(Doctor.objects.all())
    
    first_names = ["John", "Jane", "Michael", "Sarah", "David", "Emma", "Oluwaseun", "Chidinma", "Amina", "Emeka", "Fatima", "Tunde"]
    last_names = ["Smith", "Doe", "Johnson", "Okafor", "Adebayo", "Bello", "Nwachukwu", "Ibrahim", "Mohammed", "Garcia", "Olowu"]
    statuses = ["Booked", "Checked In", "Completed", "Pending"]
    payment_types = ["Private Self-Pay", "HMO Insurance"]
    payment_statuses = ["Pending", "Cleared"]
    
    bookings_to_create = []
    today = timezone.now().date()
    total_created = 0

    for i in range(1, 3001):
        ref_code = f"BK-{random.randint(100000, 999999)}-{i}"
        patient_name = f"{random.choice(first_names)} {random.choice(last_names)}"
        patient_phone = f"+23480{random.randint(10000000, 99999999)}"
        
        if doctors:
            doc = random.choice(doctors)
            doctor_id = getattr(doc, 'doc_id', str(doc.doc_id))
            doctor_name = getattr(doc, 'full_name', None) or getattr(doc, 'name', 'Specialist Doctor')
            specialty = getattr(doc, 'specialty', 'General Medicine')
        else:
            doctor_id = "doc-101"
            doctor_name = "Dr. Specialist"
            specialty = "General Medicine"
            
        delta_days = random.randint(-30, 30)
        booking_date = today + datetime.timedelta(days=delta_days)
        
        status = random.choice(statuses)
        pay_type = random.choice(payment_types)
        pay_status = "Cleared" if status == "Completed" else random.choice(payment_statuses)
        
        booking = Booking(
            ref_code=ref_code,
            patient_name=patient_name,
            patient_phone=patient_phone,
            doctor_id=doctor_id,
            doctor_name=doctor_name,
            doctor_specialty=specialty,
            date=booking_date,
            time="09:00 AM - 11:00 AM",
            status=status,
            payment_type=pay_type,
            payment_status=pay_status,
            is_active=True
        )
        bookings_to_create.append(booking)
        total_created += 1
        
        if len(bookings_to_create) >= 500:
            Booking.objects.bulk_create(bookings_to_create, ignore_conflicts=True)
            print(f"📦 Inserted batch... Total created: {total_created}/3000")
            bookings_to_create = []
            
    if bookings_to_create:
        Booking.objects.bulk_create(bookings_to_create, ignore_conflicts=True)
        print(f"📦 Inserted final batch... Total created: {total_created}/3000")
        
    print("✅ Successfully generated 3,000 test booking records!")

if __name__ == "__main__":
    generate_bookings()