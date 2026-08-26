from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from api.models import Department, Doctor, SpecialistSchedule, Role, UserProfile

class Command(BaseCommand):
    help = 'Auto-fixes and links missing ForeignKeys (Doctor.department, SpecialistSchedule.doctor, UserProfile.role) based on available parameters.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE('Auto-linking foreign key records...'))
        fixed_doctors = 0
        fixed_schedules = 0
        fixed_profiles = 0

        # 1. Fix Doctor -> Department ForeignKeys
        departments = {d.dept_id.lower(): d for d in Department.objects.all()}
        for d in Department.objects.all():
            departments[d.name.lower()] = d

        for doc in Doctor.objects.all():
            spec_lower = doc.specialty.lower()
            matched_dept = None

            if 'endocrin' in spec_lower or 'diabetes' in spec_lower or 'hormon' in spec_lower:
                matched_dept = departments.get('endocrinology')
            elif 'pulmon' in spec_lower or 'chest' in spec_lower or 'lung' in spec_lower:
                matched_dept = departments.get('pulmonology')
            elif 'nephro' in spec_lower or 'kidney' in spec_lower or 'renal' in spec_lower:
                matched_dept = departments.get('nephrology')
            elif 'haemat' in spec_lower or 'hemat' in spec_lower or 'blood' in spec_lower:
                matched_dept = departments.get('haematology')
            elif 'gastro' in spec_lower or 'digest' in spec_lower or 'liver' in spec_lower:
                matched_dept = departments.get('gastroenterology')
            elif 'rheumat' in spec_lower or 'arthritis' in spec_lower:
                matched_dept = departments.get('rheumatology')
            elif 'psychiat' in spec_lower or 'mental' in spec_lower:
                matched_dept = departments.get('psychiatry')
            elif 'diet' in spec_lower or 'nutrit' in spec_lower:
                matched_dept = departments.get('dietetics')
            elif 'physiother' in spec_lower or 'rehab' in spec_lower:
                matched_dept = departments.get('physiotherapy')
            elif 'gynae' in spec_lower or 'obs' in spec_lower or 'women' in spec_lower:
                matched_dept = departments.get('gynaecology')
            elif 'general surgery' in spec_lower or 'surgical' in spec_lower or ('surg' in spec_lower and 'ortho' not in spec_lower and 'pediat' not in spec_lower and 'neuro' not in spec_lower and 'ent' not in spec_lower):
                matched_dept = departments.get('general-surgery')
            elif 'cardio' in spec_lower or 'heart' in spec_lower:
                matched_dept = departments.get('cardiology')
            elif 'pediat' in spec_lower or 'child' in spec_lower or 'pedia' in spec_lower:
                matched_dept = departments.get('pediatrics')
            elif 'ophthalm' in spec_lower or 'eye' in spec_lower:
                matched_dept = departments.get('ophthalmology')
            elif 'orthoped' in spec_lower or 'bone' in spec_lower:
                matched_dept = departments.get('orthopedics')
            elif 'neurol' in spec_lower or 'brain' in spec_lower:
                matched_dept = departments.get('neurology')
            elif 'uro' in spec_lower:
                matched_dept = departments.get('urology')
            elif 'derma' in spec_lower or 'skin' in spec_lower:
                matched_dept = departments.get('dermatology')
            elif 'ent' in spec_lower or 'ear' in spec_lower or 'throat' in spec_lower or 'neck' in spec_lower:
                matched_dept = departments.get('ent')
            elif 'physician' in spec_lower or 'primary care' in spec_lower:
                matched_dept = departments.get('general-physician') or departments.get('general')
            elif 'general med' in spec_lower or 'outpatient' in spec_lower or 'gen' in spec_lower:
                matched_dept = departments.get('general')

            if not matched_dept:
                matched_dept = departments.get('general') or Department.objects.first()

            if matched_dept and doc.department != matched_dept:
                doc.department = matched_dept
                doc.save()
                fixed_doctors += 1
                self.stdout.write(self.style.SUCCESS(f"  [Doctor FK Fixed] Linked '{doc.full_name or doc.name}' -> Department '{matched_dept.name}' ({matched_dept.dept_id})"))

        # 2. Fix SpecialistSchedule -> Doctor ForeignKeys
        all_doctors = list(Doctor.objects.all())
        for sched in SpecialistSchedule.objects.all():
            if not sched.doctor:
                matched_doc = None
                s_name = sched.doctor_name.lower().strip() if sched.doctor_name else ''
                s_spec = sched.specialty.lower().strip() if sched.specialty else ''

                # Search by name match
                for doc in all_doctors:
                    d_full = doc.full_name.lower()
                    d_name = doc.name.lower()
                    d_acro = doc.acronym.lower()
                    if (s_name and (s_name in d_full or d_full in s_name or s_name in d_name or s_name in d_acro)):
                        matched_doc = doc
                        break

                # Search by specialty match
                if not matched_doc and s_spec:
                    for doc in all_doctors:
                        if doc.specialty.lower() == s_spec:
                            matched_doc = doc
                            break

                if matched_doc:
                    sched.doctor = matched_doc
                    if not sched.doctor_name:
                        sched.doctor_name = matched_doc.full_name or matched_doc.name
                    if not sched.specialty:
                        sched.specialty = matched_doc.specialty
                    sched.save()
                    fixed_schedules += 1
                    self.stdout.write(self.style.SUCCESS(f"  [Schedule FK Fixed] Linked Schedule '{sched.sched_id}' -> Doctor '{matched_doc.full_name or matched_doc.name}'"))

        # 3. Fix UserProfile -> Role ForeignKeys & ensure UserProfiles exist for all Users
        all_roles = {r.name.lower(): r for r in Role.objects.all()}
        super_admin_role = all_roles.get('super administrator') or Role.objects.filter(is_system_role=True).first()
        helpdesk_role = all_roles.get('helpdesk officer') or Role.objects.first()

        for user in User.objects.all():
            profile, created = UserProfile.objects.get_or_create(user=user)
            if not profile.role:
                if user.is_superuser:
                    profile.role = super_admin_role
                else:
                    profile.role = helpdesk_role
                profile.save()
                fixed_profiles += 1
                self.stdout.write(self.style.SUCCESS(f"  [UserProfile FK Fixed] Linked User '{user.username}' -> Role '{profile.role.name if profile.role else 'None'}'"))

        self.stdout.write(self.style.SUCCESS(
            f"\nCompleted! Auto-linked {fixed_doctors} doctors, {fixed_schedules} schedules, and {fixed_profiles} user profiles."
        ))
