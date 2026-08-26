from django.core.management.base import BaseCommand
from api.models import Department, Doctor, SpecialistSchedule, HmoCompany

class Command(BaseCommand):
    help = 'Seeds initial departments, doctors, HMO companies, timetables, and system users.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE('Seeding initial data for Isalu Hospitals...'))

        # 1. Departments
        departments = [
            {'dept_id': 'endocrinology', 'name': 'Endocrinology', 'description': 'Diabetes, thyroid disorders, metabolism, and hormonal balance care.', 'icon_name': 'Syringe', 'doctor_count': 3},
            {'dept_id': 'general-surgery', 'name': 'General Surgery', 'description': 'Comprehensive surgical evaluations, procedures, and post-operative care.', 'icon_name': 'Scissors', 'doctor_count': 3},
            {'dept_id': 'gynaecology', 'name': 'Obstetrics & Gynaecology', 'description': 'Women health, prenatal care, fertility, and gynecological surgeries.', 'icon_name': 'Heart', 'doctor_count': 3},
            {'dept_id': 'general-physician', 'name': 'General Physician', 'description': 'Primary healthcare, preventive medicine, and general medical consultations.', 'icon_name': 'Stethoscope', 'doctor_count': 1},
            {'dept_id': 'general', 'name': 'General Medicine & Outpatient', 'description': 'Primary healthcare, preventive medicine, and general medical outpatient care.', 'icon_name': 'UserCheck', 'doctor_count': 1},
            {'dept_id': 'pulmonology', 'name': 'Chest Physician / Pulmonology', 'description': 'Respiratory health, asthma, lung diseases, and chest consultations.', 'icon_name': 'Wind', 'doctor_count': 1},
            {'dept_id': 'cardiology', 'name': 'Cardiology', 'description': 'Heart health, hypertension management, ECG, and cardiac assessments.', 'icon_name': 'HeartPulse', 'doctor_count': 3},
            {'dept_id': 'pediatrics', 'name': 'Paediatrics / Child Health', 'description': 'Comprehensive infant, child, and adolescent healthcare services.', 'icon_name': 'Baby', 'doctor_count': 3},
            {'dept_id': 'neurology', 'name': 'Neurology', 'description': 'Brain, nerve, stroke, epilepsy, and neurological disorders treatment.', 'icon_name': 'Activity', 'doctor_count': 3},
            {'dept_id': 'orthopedics', 'name': 'Orthopedic Surgery', 'description': 'Bone fractures, joint replacements, spine care, and sports injuries.', 'icon_name': 'Stethoscope', 'doctor_count': 1},
            {'dept_id': 'urology', 'name': 'Urology', 'description': 'Urinary tract, prostate health, and male reproductive care.', 'icon_name': 'Activity', 'doctor_count': 2},
            {'dept_id': 'dermatology', 'name': 'Dermatology', 'description': 'Skin, hair, nail treatments, and cosmetic dermatology.', 'icon_name': 'ShieldCheck', 'doctor_count': 1},
            {'dept_id': 'ent', 'name': 'ENT & Head / Neck Surgery', 'description': 'Ear, nose, throat, sinus, and head/neck surgical treatments.', 'icon_name': 'Building2', 'doctor_count': 3},
            {'dept_id': 'nephrology', 'name': 'Nephrology', 'description': 'Kidney health, dialysis management, and renal care.', 'icon_name': 'Activity', 'doctor_count': 1},
            {'dept_id': 'haematology', 'name': 'Haematology', 'description': 'Blood disorders, anemia, and hematological conditions.', 'icon_name': 'Activity', 'doctor_count': 2},
            {'dept_id': 'gastroenterology', 'name': 'Gastroenterology', 'description': 'Digestive system, liver, and gastrointestinal consultations.', 'icon_name': 'Activity', 'doctor_count': 2},
            {'dept_id': 'rheumatology', 'name': 'Rheumatology', 'description': 'Joint pain, arthritis, and autoimmune disease management.', 'icon_name': 'Activity', 'doctor_count': 1},
            {'dept_id': 'psychiatry', 'name': 'Psychiatry & Mental Health', 'description': 'Mental wellness, counseling, and psychiatric evaluations.', 'icon_name': 'Activity', 'doctor_count': 1},
            {'dept_id': 'dietetics', 'name': 'Dietetics & Clinical Nutrition', 'description': 'Nutritional guidance, meal planning, and medical dietetics.', 'icon_name': 'Activity', 'doctor_count': 2},
            {'dept_id': 'physiotherapy', 'name': 'Physiotherapy & Rehabilitation', 'description': 'Physical therapy, rehabilitation, and pain management.', 'icon_name': 'Activity', 'doctor_count': 2},
            {'dept_id': 'ophthalmology', 'name': 'Ophthalmology & Eye Care', 'description': 'Comprehensive eye exams, vision care, and eye surgeries.', 'icon_name': 'Users', 'doctor_count': 1},
        ]
        for dept in departments:
            d_obj, created = Department.objects.get_or_create(dept_id=dept['dept_id'], defaults={**dept, 'status': True})
            if not created and not d_obj.status:
                d_obj.status = True
                d_obj.save()

        # 2. Doctors
        doctors = [
            {'doc_id': 'doc-1', 'name': 'Specialist A', 'full_name': 'Dr. Adewale Olusola', 'acronym': 'Specialist A', 'specialty': 'Cardiology', 'department_id': 'cardiology', 'qualification': 'MBBS, FWACS (Cardiology)', 'qualifications': 'MBBS, FWACS (Cardiology)', 'available_days': ['Monday', 'Wednesday', 'Friday'], 'time_slots': ['08:00 AM – 12:00 PM', '01:00 PM – 05:00 PM'], 'room_number': 'Suite 4B - Cardiology Wing'},
            {'doc_id': 'doc-2', 'name': 'Specialist B', 'full_name': 'Dr. Folashade Adebayo', 'acronym': 'Specialist B', 'specialty': 'General Medicine', 'department_id': 'general', 'qualification': 'MBBS, FMCP', 'qualifications': 'MBBS, FMCP', 'available_days': ['Tuesday', 'Thursday', 'Saturday'], 'time_slots': ['09:00 AM – 01:00 PM', '02:00 PM – 06:00 PM'], 'room_number': 'Room 102 - Outpatient'},
            {'doc_id': 'doc-3', 'name': 'Specialist C', 'full_name': 'Dr. Chidi Nnamdi', 'acronym': 'Specialist C', 'specialty': 'Pediatrics', 'department_id': 'pediatrics', 'qualification': 'MBBS, FWAP (Pediatrics)', 'qualifications': 'MBBS, FWAP (Pediatrics)', 'available_days': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'], 'time_slots': ['08:30 AM – 12:30 PM', '01:30 PM – 04:30 PM'], 'room_number': 'Pediatric Clinic Wing A'},
            {'doc_id': 'doc-4', 'name': 'Specialist D', 'full_name': 'Dr. Funke Akindele', 'acronym': 'Specialist D', 'specialty': 'Obstetrics & Gynecology', 'department_id': 'gynaecology', 'qualification': 'MBBS, FWACS (Obs & Gynae)', 'qualifications': 'MBBS, FWACS (Obs & Gynae)', 'available_days': ['Monday', 'Wednesday', 'Thursday'], 'time_slots': ['10:00 AM – 02:00 PM', '03:00 PM – 06:00 PM'], 'room_number': 'Maternity Suite 2'},
            {'doc_id': 'doc-5', 'name': 'Specialist E', 'full_name': 'Dr. Babatunde Lawal', 'acronym': 'Specialist E', 'specialty': 'Ophthalmology', 'department_id': 'ophthalmology', 'qualification': 'MBBS, FICO (Ophthalmology)', 'qualifications': 'MBBS, FICO (Ophthalmology)', 'available_days': ['Tuesday', 'Friday'], 'time_slots': ['08:00 AM – 01:00 PM'], 'room_number': 'Eye Clinic Room 3'},
            {'doc_id': 'doc-6', 'name': 'Specialist F', 'full_name': 'Dr. Ngozi Eze', 'acronym': 'Specialist F', 'specialty': 'Orthopedic Surgery', 'department_id': 'orthopedics', 'qualification': 'MBBS, FWACS (Orthopedics)', 'qualifications': 'MBBS, FWACS (Orthopedics)', 'available_days': ['Wednesday', 'Saturday'], 'time_slots': ['11:00 AM – 04:00 PM'], 'room_number': 'Orthopedic Wing B'},
            {'doc_id': 'doc-7', 'name': 'Specialist G', 'full_name': 'Dr. Olumide Agbaje', 'acronym': 'Specialist G', 'specialty': 'General Surgery', 'department_id': 'general-surgery', 'qualification': 'MBBS, FWACS (General Surgery)', 'qualifications': 'MBBS, FWACS (General Surgery)', 'available_days': ['Monday', 'Tuesday', 'Thursday'], 'time_slots': ['08:00 AM – 02:00 PM'], 'room_number': 'Surgical Suite 1A'},
            {'doc_id': 'doc-14', 'name': 'Specialist N', 'full_name': 'Dr. Victoria Danjuma', 'acronym': 'Specialist N', 'specialty': 'Neurology', 'department_id': 'neurology', 'qualification': 'MBBS, FMCP (Neuro)', 'qualifications': 'MBBS, FMCP (Neuro)', 'available_days': ['Monday', 'Friday'], 'time_slots': ['09:00 AM – 03:00 PM'], 'room_number': 'Neurology Clinic Wing B'},
            {'doc_id': 'doc-18', 'name': 'Specialist R', 'full_name': 'Dr. Yakubu Usman', 'acronym': 'Specialist R', 'specialty': 'Urology', 'department_id': 'urology', 'qualification': 'MBBS, FWACS (Urology)', 'qualifications': 'MBBS, FWACS (Urology)', 'available_days': ['Monday', 'Wednesday', 'Saturday'], 'time_slots': ['09:00 AM – 02:00 PM'], 'room_number': 'Urology Suite 2'},
        ]
        for doc in doctors:
            doc_data = doc.copy()
            dept_id_val = doc_data.pop('department_id', None)
            dept_obj = Department.objects.filter(dept_id=dept_id_val).first() if dept_id_val else None
            doc_data['department'] = dept_obj
            doc_obj, created = Doctor.objects.get_or_create(doc_id=doc_data['doc_id'], defaults={**doc_data, 'status': True})
            if not created and not doc_obj.status:
                doc_obj.status = True
                doc_obj.save()

        # 3. Specialist Schedules
        schedules = [
            {'sched_id': 'sched-1', 'doctor_id': 'doc-1', 'doctor_name': 'Dr. Adewale Olusola', 'specialty': 'Cardiology', 'room': 'Suite 4B - Cardiology Wing', 'duty_days': ['Mon', 'Wed', 'Fri'], 'shift_time': '08:00 AM – 02:00 PM', 'capacity': 15, 'status': True},
            {'sched_id': 'sched-2', 'doctor_id': 'doc-2', 'doctor_name': 'Dr. Folashade Adebayo', 'specialty': 'General Medicine', 'room': 'Room 102 - Outpatient', 'duty_days': ['Tue', 'Thu', 'Sat'], 'shift_time': '09:00 AM – 04:00 PM', 'capacity': 20, 'status': True},
            {'sched_id': 'sched-3', 'doctor_id': 'doc-3', 'doctor_name': 'Dr. Chidi Nnamdi', 'specialty': 'Pediatrics', 'room': 'Pediatric Clinic Wing A', 'duty_days': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'], 'shift_time': '08:30 AM – 03:30 PM', 'capacity': 18, 'status': True},
            {'sched_id': 'sched-4', 'doctor_id': 'doc-4', 'doctor_name': 'Dr. Funke Akindele', 'specialty': 'Obstetrics & Gynecology', 'room': 'Maternity Suite 2', 'duty_days': ['Mon', 'Wed', 'Thu'], 'shift_time': '10:00 AM – 05:00 PM', 'capacity': 12, 'status': True},
            {'sched_id': 'sched-5', 'doctor_id': 'doc-5', 'doctor_name': 'Dr. Babatunde Lawal', 'specialty': 'Ophthalmology', 'room': 'Eye Clinic Room 3', 'duty_days': ['Tue', 'Fri'], 'shift_time': '08:00 AM – 01:00 PM', 'capacity': 15, 'status': True},
            {'sched_id': 'sched-6', 'doctor_id': 'doc-6', 'doctor_name': 'Dr. Ngozi Eze', 'specialty': 'Orthopedic Surgery', 'room': 'Orthopedic Wing B', 'duty_days': ['Wed', 'Sat'], 'shift_time': '11:00 AM – 06:00 PM', 'capacity': 10, 'status': True},
        ]
        for sched in schedules:
            sched_data = sched.copy()
            doc_id_val = sched_data.pop('doctor_id', None)
            doc_obj = Doctor.objects.filter(doc_id=doc_id_val).first() if doc_id_val else None
            sched_data['doctor'] = doc_obj
            SpecialistSchedule.objects.get_or_create(sched_id=sched_data['sched_id'], defaults=sched_data)

        # 4. HMO Companies
        hmos = [
            {'hmo_id': 'hmo-1', 'name': 'Hygeia HMO', 'code': 'HYG-9021', 'email': 'preauth@hygeiahmo.com', 'phone': '+234 700 494 342', 'contact_person': 'Mrs. Toyin Adeyemi', 'status': True},
            {'hmo_id': 'hmo-2', 'name': 'Reliance HMO', 'code': 'REL-4412', 'email': 'claims@reliancehmo.com', 'phone': '+234 1 700 1570', 'contact_person': 'Mr. Femi Ogunleye', 'status': True},
            {'hmo_id': 'hmo-3', 'name': 'AXA Mansard Health', 'code': 'AXA-8819', 'email': 'care@axamansard.com', 'phone': '+234 1 448 5433', 'contact_person': 'Dr. Sandra Okafor', 'status': True},
            {'hmo_id': 'hmo-4', 'name': 'Leadway Health HMO', 'code': 'LAD-3011', 'email': 'medical@leadwayhealth.com', 'phone': '+234 1 280 1000', 'contact_person': 'Mr. Segun Alabi', 'status': True},
            {'hmo_id': 'hmo-5', 'name': 'Avon HMO', 'code': 'AVN-5520', 'email': 'approvals@avonhmo.com', 'phone': '+234 700 286 6466', 'contact_person': 'Mrs. Chidimma Nwosu', 'status': True},
        ]
        for hmo in hmos:
            HmoCompany.objects.get_or_create(hmo_id=hmo['hmo_id'], defaults=hmo)

        # 5. User Roles
        from api.models import Role
        roles = [
            {'role_id': 'role-1', 'name': 'Super Administrator', 'description': 'Full administrative control over all hospital operations, user management, and clinic configurations.', 'primary_desk': 'analytics', 'allowed_desks': ['helpdesk', 'hmo', 'cashdesk', 'analytics', 'monitor', 'users', 'clinic', 'all_patients', 'checked_in_patients', 'hmo_enrollees', 'private_patients', 'create_specialist_schedule', 'disabled_bookings'], 'is_system_role': True, 'status': True},
            {'role_id': 'role-2', 'name': 'Helpdesk Officer', 'description': 'Front-desk reception patient intake, queue checking, appointment registration, and patient directories.', 'primary_desk': 'helpdesk', 'allowed_desks': ['helpdesk', 'all_patients', 'checked_in_patients'], 'is_system_role': True, 'status': True},
            {'role_id': 'role-3', 'name': 'HMO Approval Officer', 'description': 'Verification of HMO insurance policies, authorization coding, and HMO enrollees directory.', 'primary_desk': 'hmo', 'allowed_desks': ['hmo', 'hmo_enrollees'], 'is_system_role': True, 'status': True},
            {'role_id': 'role-4', 'name': 'Cashdesk Billing Officer', 'description': 'Private self-pay payments clearance, billing invoicing, POS transactions, and private patient directory.', 'primary_desk': 'cashdesk', 'allowed_desks': ['cashdesk', 'private_patients'], 'is_system_role': True, 'status': True},
            {'role_id': 'role-5', 'name': 'Monitor Desk Operator', 'description': 'Real-time consultation queue monitoring and live TV display management.', 'primary_desk': 'monitor', 'allowed_desks': ['monitor'], 'is_system_role': True, 'status': True},
            {'role_id': 'role-6', 'name': 'Queue Analytics Officer', 'description': 'Executive intelligence, department throughput metrics, and AI board summaries.', 'primary_desk': 'analytics', 'allowed_desks': ['analytics'], 'is_system_role': True, 'status': True},
        ]
        for r in roles:
            Role.objects.update_or_create(
                role_id=r['role_id'],
                defaults=r
            )

        # 6. Django Auth Users & UserProfiles
        from django.contrib.auth.models import User
        from api.models import UserProfile
        users = [
            {'name': 'Dr. Chief Administrator', 'email': 'admin@isaluhospitals.com', 'raw_password': 'admin123', 'role': 'Super Administrator', 'desk': 'All Access', 'last_active': 'Just now'},
            {'name': 'Mrs. Adesuwa Receptionist', 'email': 'reception@isaluhospitals.com', 'raw_password': 'admin123', 'role': 'Helpdesk Officer', 'desk': 'Helpdesk Reception', 'last_active': '5 mins ago'},
            {'name': 'Mr. Kunle HMO Officer', 'email': 'hmo.desk@isaluhospitals.com', 'raw_password': 'admin123', 'role': 'HMO Approval Officer', 'desk': 'HMO Approval Desk', 'last_active': '12 mins ago'},
            {'name': 'Mrs. Blessing Cashier', 'email': 'cashdesk@isaluhospitals.com', 'raw_password': 'admin123', 'role': 'Cashdesk Billing Officer', 'desk': 'Cashdesk & Invoicing', 'last_active': '20 mins ago'},
        ]
        for u in users:
            clean_email = u['email'].lower()
            role_obj = Role.objects.filter(name__iexact=u['role']).first()

            auth_user = User.objects.filter(email__iexact=clean_email).first()
            if not auth_user:
                auth_user = User.objects.create_user(
                    username=clean_email,
                    email=clean_email,
                    password=u['raw_password'],
                    first_name=u['name'],
                    is_staff=True,
                    is_superuser=(u['role'] == 'Super Administrator')
                )
            else:
                auth_user.set_password(u['raw_password'])
                auth_user.first_name = u['name']
                auth_user.is_staff = True
                auth_user.is_superuser = (u['role'] == 'Super Administrator')
                auth_user.save()

            UserProfile.objects.update_or_create(
                user=auth_user,
                defaults={
                    'role': role_obj,
                }
            )

        self.stdout.write(self.style.SUCCESS('Successfully seeded initial Django database!'))


