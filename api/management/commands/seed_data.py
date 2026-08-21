from django.core.management.base import BaseCommand
from api.models import Department, Doctor, SpecialistSchedule, HmoCompany, SystemUser

class Command(BaseCommand):
    help = 'Seeds initial departments, doctors, HMO companies, timetables, and system users.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE('Seeding initial data for Isalu Hospitals...'))

        # 1. Departments
        departments = [
            {'dept_id': 'cardiology', 'name': 'Cardiology', 'description': 'Heart care and cardiovascular consultations.', 'icon_name': 'HeartPulse', 'doctor_count': 2},
            {'dept_id': 'pediatrics', 'name': 'Pediatrics & Child Care', 'description': 'Medical care for infants and children.', 'icon_name': 'Baby', 'doctor_count': 2},
            {'dept_id': 'neurology', 'name': 'Neurology & Brain Health', 'description': 'Brain and nerve specialist clinic.', 'icon_name': 'Activity', 'doctor_count': 1},
            {'dept_id': 'orthopedics', 'name': 'Orthopedics & Joint Care', 'description': 'Bone, joint, and spine surgery.', 'icon_name': 'Stethoscope', 'doctor_count': 1},
            {'dept_id': 'dermatology', 'name': 'Dermatology & Skin', 'description': 'Skin, hair, and aesthetic care.', 'icon_name': 'ShieldCheck', 'doctor_count': 1},
            {'dept_id': 'ophthalmology', 'name': 'Ophthalmology & Eye Care', 'description': 'Comprehensive eye and vision clinic.', 'icon_name': 'Users', 'doctor_count': 1},
            {'dept_id': 'ent', 'name': 'ENT & Head/Neck', 'description': 'Ear, nose, and throat treatments.', 'icon_name': 'Building2', 'doctor_count': 1},
            {'dept_id': 'general', 'name': 'General Medicine & Outpatient', 'description': 'Primary care and health checkups.', 'icon_name': 'UserCheck', 'doctor_count': 3},
        ]
        for dept in departments:
            Department.objects.get_or_create(dept_id=dept['dept_id'], defaults=dept)

        # 2. Doctors
        doctors = [
            {'doc_id': 'doc-1', 'name': 'Specialist A', 'full_name': 'Dr. Adewale Olusola', 'acronym': 'Specialist A', 'specialty': 'Cardiology', 'department_id': 'cardiology', 'qualification': 'MBBS, FWACS (Cardiology)', 'qualifications': 'MBBS, FWACS (Cardiology)', 'available_days': ['Monday', 'Wednesday', 'Friday'], 'time_slots': ['08:00 AM – 12:00 PM', '01:00 PM – 05:00 PM'], 'room_number': 'Suite 4B - Cardiology Wing'},
            {'doc_id': 'doc-2', 'name': 'Specialist B', 'full_name': 'Dr. Folashade Adebayo', 'acronym': 'Specialist B', 'specialty': 'General Medicine', 'department_id': 'general', 'qualification': 'MBBS, FMCP', 'qualifications': 'MBBS, FMCP', 'available_days': ['Tuesday', 'Thursday', 'Saturday'], 'time_slots': ['09:00 AM – 01:00 PM', '02:00 PM – 06:00 PM'], 'room_number': 'Room 102 - Outpatient'},
            {'doc_id': 'doc-3', 'name': 'Specialist C', 'full_name': 'Dr. Chidi Nnamdi', 'acronym': 'Specialist C', 'specialty': 'Pediatrics', 'department_id': 'pediatrics', 'qualification': 'MBBS, FWAP (Pediatrics)', 'qualifications': 'MBBS, FWAP (Pediatrics)', 'available_days': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'], 'time_slots': ['08:30 AM – 12:30 PM', '01:30 PM – 04:30 PM'], 'room_number': 'Pediatric Clinic Wing A'},
            {'doc_id': 'doc-4', 'name': 'Specialist D', 'full_name': 'Dr. Funke Akindele', 'acronym': 'Specialist D', 'specialty': 'Obstetrics & Gynecology', 'department_id': 'pediatrics', 'qualification': 'MBBS, FWACS (Obs & Gynae)', 'qualifications': 'MBBS, FWACS (Obs & Gynae)', 'available_days': ['Monday', 'Wednesday', 'Thursday'], 'time_slots': ['10:00 AM – 02:00 PM', '03:00 PM – 06:00 PM'], 'room_number': 'Maternity Suite 2'},
            {'doc_id': 'doc-5', 'name': 'Specialist E', 'full_name': 'Dr. Babatunde Lawal', 'acronym': 'Specialist E', 'specialty': 'Ophthalmology', 'department_id': 'ophthalmology', 'qualification': 'MBBS, FICO (Ophthalmology)', 'qualifications': 'MBBS, FICO (Ophthalmology)', 'available_days': ['Tuesday', 'Friday'], 'time_slots': ['08:00 AM – 01:00 PM'], 'room_number': 'Eye Clinic Room 3'},
            {'doc_id': 'doc-6', 'name': 'Specialist F', 'full_name': 'Dr. Ngozi Eze', 'acronym': 'Specialist F', 'specialty': 'Orthopedic Surgery', 'department_id': 'orthopedics', 'qualification': 'MBBS, FWACS (Orthopedics)', 'qualifications': 'MBBS, FWACS (Orthopedics)', 'available_days': ['Wednesday', 'Saturday'], 'time_slots': ['11:00 AM – 04:00 PM'], 'room_number': 'Orthopedic Wing B'},
        ]
        for doc in doctors:
            Doctor.objects.get_or_create(doc_id=doc['doc_id'], defaults=doc)

        # 3. Specialist Schedules
        schedules = [
            {'sched_id': 'sched-1', 'doctor_id': 'doc-1', 'doctor_name': 'Dr. Adewale Olusola', 'specialty': 'Cardiology', 'room': 'Suite 4B - Cardiology Wing', 'duty_days': ['Mon', 'Wed', 'Fri'], 'shift_time': '08:00 AM – 02:00 PM', 'capacity': 15, 'status': 'Active On Duty'},
            {'sched_id': 'sched-2', 'doctor_id': 'doc-2', 'doctor_name': 'Dr. Folashade Adebayo', 'specialty': 'General Medicine', 'room': 'Room 102 - Outpatient', 'duty_days': ['Tue', 'Thu', 'Sat'], 'shift_time': '09:00 AM – 04:00 PM', 'capacity': 20, 'status': 'Active On Duty'},
            {'sched_id': 'sched-3', 'doctor_id': 'doc-3', 'doctor_name': 'Dr. Chidi Nnamdi', 'specialty': 'Pediatrics', 'room': 'Pediatric Clinic Wing A', 'duty_days': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'], 'shift_time': '08:30 AM – 03:30 PM', 'capacity': 18, 'status': 'Active On Duty'},
            {'sched_id': 'sched-4', 'doctor_id': 'doc-4', 'doctor_name': 'Dr. Funke Akindele', 'specialty': 'Obstetrics & Gynecology', 'room': 'Maternity Suite 2', 'duty_days': ['Mon', 'Wed', 'Thu'], 'shift_time': '10:00 AM – 05:00 PM', 'capacity': 12, 'status': 'Active On Duty'},
            {'sched_id': 'sched-5', 'doctor_id': 'doc-5', 'doctor_name': 'Dr. Babatunde Lawal', 'specialty': 'Ophthalmology', 'room': 'Eye Clinic Room 3', 'duty_days': ['Tue', 'Fri'], 'shift_time': '08:00 AM – 01:00 PM', 'capacity': 15, 'status': 'Active On Duty'},
            {'sched_id': 'sched-6', 'doctor_id': 'doc-6', 'doctor_name': 'Dr. Ngozi Eze', 'specialty': 'Orthopedic Surgery', 'room': 'Orthopedic Wing B', 'duty_days': ['Wed', 'Sat'], 'shift_time': '11:00 AM – 06:00 PM', 'capacity': 10, 'status': 'Active On Duty'},
        ]
        for sched in schedules:
            SpecialistSchedule.objects.get_or_create(sched_id=sched['sched_id'], defaults=sched)

        # 4. HMO Companies
        hmos = [
            {'hmo_id': 'hmo-1', 'name': 'Hygeia HMO', 'code': 'HYG-9021', 'email': 'preauth@hygeiahmo.com', 'phone': '+234 700 494 342', 'contact_person': 'Mrs. Toyin Adeyemi', 'status': 'Active Partner'},
            {'hmo_id': 'hmo-2', 'name': 'Reliance HMO', 'code': 'REL-4412', 'email': 'claims@reliancehmo.com', 'phone': '+234 1 700 1570', 'contact_person': 'Mr. Femi Ogunleye', 'status': 'Active Partner'},
            {'hmo_id': 'hmo-3', 'name': 'AXA Mansard Health', 'code': 'AXA-8819', 'email': 'care@axamansard.com', 'phone': '+234 1 448 5433', 'contact_person': 'Dr. Sandra Okafor', 'status': 'Active Partner'},
            {'hmo_id': 'hmo-4', 'name': 'Leadway Health HMO', 'code': 'LAD-3011', 'email': 'medical@leadwayhealth.com', 'phone': '+234 1 280 1000', 'contact_person': 'Mr. Segun Alabi', 'status': 'Active Partner'},
            {'hmo_id': 'hmo-5', 'name': 'Avon HMO', 'code': 'AVN-5520', 'email': 'approvals@avonhmo.com', 'phone': '+234 700 286 6466', 'contact_person': 'Mrs. Chidimma Nwosu', 'status': 'Active Partner'},
        ]
        for hmo in hmos:
            HmoCompany.objects.get_or_create(hmo_id=hmo['hmo_id'], defaults=hmo)

        # 5. System Users
        users = [
            {'user_id': 'usr-1', 'name': 'Dr. Chief Administrator', 'email': 'admin@isaluhospitals.com', 'role': 'Super Administrator', 'desk': 'All Access', 'status': 'Active', 'last_active': 'Just now'},
            {'user_id': 'usr-2', 'name': 'Mrs. Adesuwa Receptionist', 'email': 'reception@isaluhospitals.com', 'role': 'Helpdesk Officer', 'desk': 'Helpdesk Reception', 'status': 'Active', 'last_active': '5 mins ago'},
            {'user_id': 'usr-3', 'name': 'Mr. Kunle HMO Officer', 'email': 'hmo.desk@isaluhospitals.com', 'role': 'HMO Approval Officer', 'desk': 'HMO Approval Desk', 'status': 'Active', 'last_active': '12 mins ago'},
            {'user_id': 'usr-4', 'name': 'Mrs. Blessing Cashier', 'email': 'cashdesk@isaluhospitals.com', 'role': 'Cashier / Billing Officer', 'desk': 'Cashdesk & Invoicing', 'status': 'Active', 'last_active': '20 mins ago'},
        ]
        for u in users:
            SystemUser.objects.get_or_create(user_id=u['user_id'], defaults=u)

        self.stdout.write(self.style.SUCCESS('Successfully seeded initial Django database!'))
