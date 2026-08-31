from rest_framework import serializers
from django.contrib.auth.models import User
import time
from .models import Department, Doctor, SpecialistSchedule, Booking, HmoCompany, CustomTimeSlot, Role, UserProfile

def parse_bool_status(val, default=True):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ('true', '1', 'active', 'active partner', 'active on duty', 'confirmed', 'yes'):
        return True
    if s in ('false', '0', 'inactive', 'disabled', 'off duty', 'cancelled', 'no', 'maintenance', 'under maintenance'):
        return False
    return default


class DepartmentSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='dept_id', read_only=True)
    
    class Meta:
        model = Department
        fields = '__all__'

    def to_internal_value(self, data):
        data_copy = data.copy() if hasattr(data, 'copy') else dict(data)
        if 'status' in data_copy:
            data_copy['status'] = parse_bool_status(data_copy['status'])
        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.dept_id
        ret['dept_id'] = instance.dept_id
        ret['status'] = instance.status
        ret['location'] = getattr(instance, 'location', None) or 'Main Building'
        doc_count = instance.doctors.count()
        ret['doctor_count'] = doc_count if doc_count > 0 else (instance.doctor_count or 0)
        ret['doctorCount'] = ret['doctor_count']
        return ret


class DoctorSerializer(serializers.ModelSerializer):
    doc_id = serializers.CharField(required=False)
    name = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Doctor
        fields = '__all__'

    def to_internal_value(self, data):
        import time
        data_copy = data.copy() if hasattr(data, 'copy') else dict(data)

        if self.instance:
            doc_id_val = getattr(self.instance, 'doc_id', None) or data_copy.get('doc_id') or data_copy.get('id')
        else:
            doc_id_val = data_copy.get('doc_id') or data_copy.get('id') or f"doc-{int(time.time() * 1000)}"

        if doc_id_val:
            data_copy['doc_id'] = doc_id_val

        if not data_copy.get('name') and not self.instance:
            data_copy['name'] = data_copy.get('fullName') or data_copy.get('full_name') or 'Doctor'

        mapping = {
            'fullName': 'full_name',
            'availableDays': 'available_days',
            'availability': 'available_days',
            'timeSlots': 'time_slots',
            'roomNumber': 'room_number',
            'room': 'room_number',
            'acceptedPatientTypes': 'accepted_patient_types',
            'accepted_patient_types': 'accepted_patient_types',
        }
        for camel, snake in mapping.items():
            if camel in data_copy:
                if snake not in data_copy or not data_copy[snake]:
                    data_copy[snake] = data_copy[camel]

        dept_val = data_copy.get('department_id') or data_copy.get('departmentId') or data_copy.get('department')
        if dept_val:
            if isinstance(dept_val, dict):
                dept_str = str(dept_val.get('dept_id') or dept_val.get('id') or dept_val.get('name') or '').strip()
            else:
                dept_str = str(dept_val).strip()
            dept_obj = Department.objects.filter(dept_id__iexact=dept_str).first() if dept_str else None
            if not dept_obj and dept_str:
                dept_obj = Department.objects.filter(name__icontains=dept_str).first()
            if dept_obj:
                data_copy['department'] = dept_obj.dept_id
            else:
                if self.instance and self.instance.department:
                    data_copy['department'] = self.instance.department.dept_id
                else:
                    data_copy['department'] = None
        elif self.instance and self.instance.department:
            data_copy['department'] = self.instance.department.dept_id

        if 'status' in data_copy:
            data_copy['status'] = parse_bool_status(data_copy['status'])

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.doc_id
        ret['doc_id'] = instance.doc_id
        ret['fullName'] = instance.full_name or instance.name
        ret['full_name'] = instance.full_name or instance.name
        ret['departmentId'] = instance.department.dept_id if instance.department else (getattr(instance, 'department_id', None) or '')
        ret['department_id'] = ret['departmentId']
        if instance.department:
            ret['department'] = {
                'dept_id': instance.department.dept_id,
                'id': instance.department.dept_id,
                'name': instance.department.name,
                'description': instance.department.description,
                'icon_name': instance.department.icon_name,
            }
        else:
            ret['department'] = None
        ret['availableDays'] = instance.available_days or []
        ret['availability'] = instance.available_days or []
        ret['timeSlots'] = instance.time_slots or []
        ret['roomNumber'] = instance.room_number or ''
        ret['room'] = instance.room_number or ''
        types = instance.accepted_patient_types
        if not types or len(types) == 0:
            types = ["Private Self-Pay", "HMO Insurance"]
        ret['acceptedPatientTypes'] = types
        ret['accepted_patient_types'] = types
        ret['status'] = instance.status
        return ret


class SpecialistScheduleSerializer(serializers.ModelSerializer):
    sched_id = serializers.CharField(required=False)

    class Meta:
        model = SpecialistSchedule
        fields = '__all__'

    def to_internal_value(self, data):
        import time
        data_copy = data.copy() if hasattr(data, 'copy') else dict(data)

        if self.instance:
            sched_id_val = getattr(self.instance, 'sched_id', None) or data_copy.get('sched_id') or data_copy.get('id')
        else:
            sched_id_val = data_copy.get('sched_id') or data_copy.get('id') or f"sched-{int(time.time() * 1000)}"

        if sched_id_val:
            data_copy['sched_id'] = sched_id_val

        mapping = {
            'doctorName': 'doctor_name',
            'dutyDays': 'duty_days',
            'dayConfigs': 'day_configs',
            'shiftTime': 'shift_time',
            'totalWeeklyCapacity': 'total_weekly_capacity',
        }
        for camel, snake in mapping.items():
            if camel in data_copy:
                if snake not in data_copy or not data_copy[snake]:
                    data_copy[snake] = data_copy[camel]

        doc_val = data_copy.get('doctor_id') or data_copy.get('doctorId') or data_copy.get('doctor')
        if doc_val:
            if isinstance(doc_val, dict):
                doc_str = str(doc_val.get('doc_id') or doc_val.get('id') or doc_val.get('name') or '').strip()
            else:
                doc_str = str(doc_val).strip()
            doc_obj = Doctor.objects.filter(doc_id__iexact=doc_str).first() if doc_str else None
            if not doc_obj and doc_str:
                doc_obj = Doctor.objects.filter(name__iexact=doc_str).first() or Doctor.objects.filter(full_name__iexact=doc_str).first()
            if doc_obj:
                data_copy['doctor'] = doc_obj.doc_id
            else:
                if self.instance and self.instance.doctor:
                    data_copy['doctor'] = self.instance.doctor.doc_id
                else:
                    data_copy['doctor'] = None
        elif self.instance and self.instance.doctor:
            data_copy['doctor'] = self.instance.doctor.doc_id

        if 'status' in data_copy:
            data_copy['status'] = parse_bool_status(data_copy['status'])

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        doc_id_val = instance.doctor.doc_id if instance.doctor else ''
        doc_name_val = instance.doctor_name
        specialty_val = instance.specialty
        ret['id'] = instance.sched_id
        ret['sched_id'] = instance.sched_id
        ret['doctorId'] = doc_id_val
        ret['doctor_id'] = doc_id_val
        ret['doctorName'] = doc_name_val
        ret['doctor_name'] = doc_name_val
        ret['specialty'] = specialty_val
        ret['dutyDays'] = instance.duty_days or []
        ret['duty_days'] = instance.duty_days or []
        ret['dayConfigs'] = instance.day_configs or {}
        ret['day_configs'] = instance.day_configs or {}
        ret['shiftTime'] = instance.shift_time
        ret['shift_time'] = instance.shift_time
        ret['totalWeeklyCapacity'] = instance.total_weekly_capacity or instance.capacity
        ret['total_weekly_capacity'] = instance.total_weekly_capacity or instance.capacity
        ret['status'] = instance.status
        return ret


class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = '__all__'

    def to_internal_value(self, data):
        data_copy = data.copy() if hasattr(data, 'copy') else dict(data)
        mapping = {
            'refCode': 'ref_code',
            'doctorId': 'doctor_id',
            'doctorName': 'doctor_name',
            'doctorSpecialty': 'doctor_specialty',
            'patientName': 'patient_name',
            'patientPhone': 'patient_phone',
            'patientEmail': 'patient_email',
            'paymentType': 'payment_type',
            'hmoName': 'hmo_name',
            'hmoPolicyCode': 'hmo_policy_code',
            'hmoAuthCode': 'hmo_auth_code',
            'referralDocName': 'referral_doc_name',
            'hmoStatus': 'hmo_status',
            'paymentStatus': 'payment_status',
            'paymentMethod': 'payment_method',
            'invoiceRef': 'invoice_ref',
            'isActive': 'is_active',
            'deleteReason': 'delete_reason',
        }
        for camel, snake in mapping.items():
            if camel in data_copy:
                if snake not in data_copy or data_copy[snake] is None or data_copy[snake] == '':
                    data_copy[snake] = data_copy[camel]

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['refCode'] = instance.ref_code
        ret['doctorId'] = instance.doctor_id
        ret['doctorName'] = instance.doctor_name
        ret['doctorSpecialty'] = instance.doctor_specialty
        ret['patientName'] = instance.patient_name
        ret['patientPhone'] = instance.patient_phone
        ret['patientEmail'] = instance.patient_email
        ret['paymentType'] = instance.payment_type
        ret['hmoName'] = instance.hmo_name
        ret['hmoPolicyCode'] = instance.hmo_policy_code
        ret['hmoAuthCode'] = instance.hmo_auth_code
        ret['referralDocName'] = instance.referral_doc_name
        ret['hmoStatus'] = instance.hmo_status
        ret['paymentStatus'] = instance.payment_status
        ret['paymentMethod'] = instance.payment_method
        ret['invoiceRef'] = instance.invoice_ref
        ret['isActive'] = instance.is_active
        ret['deleteReason'] = instance.delete_reason
        ret['createdAt'] = instance.created_at.isoformat() if instance.created_at else None
        return ret

    def validate(self, data):
        data = super().validate(data)
        date_str = data.get('date')
        time_str = data.get('time')
        doc_id = data.get('doctor_id') or data.get('doctorId') or data.get('doctor')
        doc_name = data.get('doctor_name') or data.get('doctorName')

        # 1. Backend Inactive Schedule & Status Validation
        doc_obj = None
        if doc_id:
            if isinstance(doc_id, Doctor):
                doc_obj = doc_id
            else:
                doc_obj = Doctor.objects.filter(doc_id__iexact=str(doc_id).strip()).first()
        if not doc_obj and doc_name:
            doc_obj = Doctor.objects.filter(name__iexact=str(doc_name).strip()).first() or Doctor.objects.filter(full_name__iexact=str(doc_name).strip()).first()

        if doc_obj:
            if not doc_obj.status:
                raise serializers.ValidationError({
                    "error": f"Doctor Profile Inactive: {doc_obj.full_name or doc_obj.name} is currently inactive or unavailable for appointments."
                })
            sched_obj = doc_obj.schedules.first()
            if sched_obj and not sched_obj.status:
                raise serializers.ValidationError({
                    "error": f"Schedule Suspended: Clinic schedule for {doc_obj.full_name or doc_obj.name} is currently suspended or on leave."
                })

            # 2. Backend Daily Shift Capacity Validation
            if date_str:
                from .models import Booking
                import datetime

                dt_obj = None
                try:
                    dt_obj = datetime.datetime.strptime(str(date_str), '%Y-%m-%d')
                except:
                    pass

                day_short = dt_obj.strftime('%a') if dt_obj else ''
                max_capacity = 15
                if sched_obj:
                    day_cfgs = sched_obj.day_configs or {}
                    if day_short in day_cfgs and isinstance(day_cfgs[day_short], dict):
                        max_capacity = int(day_cfgs[day_short].get('capacity') or sched_obj.capacity or 15)
                    elif sched_obj.capacity:
                        max_capacity = int(sched_obj.capacity)

                existing_count = Booking.objects.filter(
                    doctor_name=doc_obj.full_name or doc_obj.name,
                    date=date_str,
                    is_active=True
                ).exclude(status="Disabled").count()

                if existing_count >= max_capacity:
                    raise serializers.ValidationError({
                        "error": f"Daily Shift Capacity Full: {doc_obj.full_name or doc_obj.name} has reached maximum daily patient capacity ({max_capacity} visits) for {date_str}. Please select another date."
                    })

        if date_str and time_str:
            import datetime, re
            from django.utils import timezone

            now_local = timezone.localtime(timezone.now())
            today_str = now_local.strftime('%Y-%m-%d')

            if date_str == today_str:
                match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)?', str(time_str), re.IGNORECASE)
                if match:
                    hour = int(match.group(1))
                    minute = int(match.group(2))
                    ampm = match.group(3)
                    if ampm:
                        ampm = ampm.upper()
                        if ampm == 'PM' and hour < 12:
                            hour += 12
                        elif ampm == 'AM' and hour == 12:
                            hour = 0

                    clinic_start = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    time_diff_minutes = (clinic_start - now_local).total_seconds() / 60.0

                    if time_diff_minutes < 30:
                        raise serializers.ValidationError({
                            "error": "Same-Day Cutoff Restriction: Online bookings for today's clinic must be placed at least 30 minutes prior to the clinic start time. Please select a future date or contact hospital reception."
                        })

        return data

    def create(self, validated_data):
        import random
        if not validated_data.get('ref_code'):
            validated_data['ref_code'] = f"ISALU-{random.randint(10000, 99999)}"
        return super().create(validated_data)


class HmoCompanySerializer(serializers.ModelSerializer):
    hmo_id = serializers.CharField(required=False)
    name = serializers.CharField(required=False)

    class Meta:
        model = HmoCompany
        fields = '__all__'

    def to_internal_value(self, data):
        import time
        data_copy = data.copy() if hasattr(data, 'copy') else dict(data)

        if self.instance:
            hmo_id_val = getattr(self.instance, 'hmo_id', None) or data_copy.get('hmo_id') or data_copy.get('id')
        else:
            hmo_id_val = data_copy.get('hmo_id') or data_copy.get('id') or f"hmo-{int(time.time() * 1000)}"

        if hmo_id_val:
            data_copy['hmo_id'] = hmo_id_val

        contact = data_copy.get('contact_person') or data_copy.get('contactPerson') or 'Pre-Auth Desk Officer'
        data_copy['contact_person'] = contact

        mapping = {
            'contactPerson': 'contact_person',
        }
        for camel, snake in mapping.items():
            if camel in data_copy:
                if snake not in data_copy or not data_copy[snake]:
                    data_copy[snake] = data_copy[camel]
                data_copy.pop(camel, None)

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.hmo_id
        ret['contactPerson'] = instance.contact_person
        return ret


class SystemUserSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    user_id = serializers.SerializerMethodField()
    name = serializers.CharField(source='first_name', required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False)
    role = serializers.CharField(required=False, allow_blank=True)
    desk = serializers.CharField(required=False, allow_blank=True)
    status = serializers.SerializerMethodField()
    last_active = serializers.SerializerMethodField()
    lastActive = serializers.SerializerMethodField()
    last_login = serializers.SerializerMethodField()
    lastLogin = serializers.SerializerMethodField()
    created_at = serializers.SerializerMethodField()
    createdAt = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'user_id', 'name', 'email', 'password', 'role', 'desk', 'status',
            'last_active', 'lastActive', 'last_login', 'lastLogin', 'created_at', 'createdAt'
        ]

    def get_id(self, obj):
        return f"usr-{obj.id}"

    def get_user_id(self, obj):
        return f"usr-{obj.id}"

    def get_status(self, obj):
        return 'Active' if obj.is_active else 'Disabled'

    def get_last_login(self, obj):
        if obj.last_login:
            return obj.last_login.strftime("%Y-%m-%d %H:%M:%S")
        if obj.date_joined:
            return obj.date_joined.strftime("%Y-%m-%d %H:%M:%S")
        return 'Never logged in'

    def get_lastLogin(self, obj):
        return self.get_last_login(obj)

    def get_last_active(self, obj):
        return self.get_last_login(obj)

    def get_lastActive(self, obj):
        return self.get_last_login(obj)

    def get_created_at(self, obj):
        if obj.date_joined:
            return obj.date_joined.strftime("%Y-%m-%d %H:%M:%S")
        return ''

    def get_createdAt(self, obj):
        return self.get_created_at(obj)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret.pop('password', None)
        ret.pop('password_hash', None)
        ret.pop('user_password', None)
        role_name = 'Helpdesk Officer'
        desk_name = 'helpdesk'

        if hasattr(instance, 'profile') and instance.profile and instance.profile.role:
            role_name = instance.profile.role.name
            desk_name = instance.profile.role.primary_desk
        elif instance.is_superuser:
            role_name = 'Super Administrator'
            desk_name = 'analytics'

        ret['role'] = role_name
        ret['desk'] = desk_name
        ret['name'] = instance.first_name or instance.username
        return ret

    def create(self, validated_data):
        import time
        initial_data = self.initial_data or {}
        raw_password = validated_data.get('password') or initial_data.get('password') or 'admin123'
        email = (validated_data.get('email') or initial_data.get('email') or '').strip().lower()
        name = (
            validated_data.get('first_name')
            or initial_data.get('name')
            or (email.split('@')[0] if email else 'Staff User')
        )
        role_name = initial_data.get('role') or 'Helpdesk Officer'
        status_input = initial_data.get('status', 'Active')

        clean_username = email if email else f"user_{int(time.time() * 1000)}".lower()

        # Check if user already exists in User table
        user = User.objects.filter(email__iexact=email).first() if email else None
        if not user:
            user = User.objects.filter(username__iexact=clean_username).first()

        if user:
            user.first_name = name
            if raw_password:
                user.set_password(raw_password)
            user.is_staff = True
            user.is_active = (status_input != 'Disabled')
            user.save()
        else:
            user = User.objects.create_user(
                username=clean_username,
                email=email,
                password=raw_password,
                first_name=name,
                is_staff=True,
                is_active=(status_input != 'Disabled')
            )

        role_obj = self._resolve_or_create_role(role_name)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if role_obj:
            profile.role = role_obj
            profile.save()

        return user

    def _resolve_or_create_role(self, role_name):
        if not role_name:
            return None
        r_clean = str(role_name).strip()
        role_obj = (
            Role.objects.filter(name__iexact=r_clean).first()
            or Role.objects.filter(role_id__iexact=r_clean).first()
            or Role.objects.filter(name__icontains=r_clean).first()
        )
        if not role_obj:
            lower = r_clean.lower()
            if 'monitor' in lower or 'controller' in lower:
                primary = 'monitor'
            elif 'hmo' in lower or 'insurance' in lower:
                primary = 'hmo'
            elif 'cash' in lower or 'billing' in lower:
                primary = 'cashdesk'
            elif 'analytics' in lower or 'executive' in lower:
                primary = 'analytics'
            else:
                primary = 'helpdesk'

            import time
            role_obj = Role.objects.create(
                role_id=f"role-{int(time.time() * 1000)}",
                name=r_clean,
                description=f"Custom role: {r_clean}",
                primary_desk=primary,
                allowed_desks=[primary],
                is_system_role=False,
                status=True
            )
        return role_obj

    def update(self, instance, validated_data):
        initial_data = self.initial_data or {}

        email = (validated_data.get('email') or initial_data.get('email') or '').strip().lower()
        if email:
            instance.email = email
            instance.username = email

        name = validated_data.get('first_name') or initial_data.get('name')
        if name:
            instance.first_name = name

        password = validated_data.get('password') or initial_data.get('password')
        if password and not (password.startswith('pbkdf2_') or password.startswith('argon2')):
            instance.set_password(password)

        status_input = initial_data.get('status')
        if status_input:
            instance.is_active = (status_input != 'Disabled')

        instance.save()

        profile, _ = UserProfile.objects.get_or_create(user=instance)
        role_name = initial_data.get('role')
        if role_name:
            role_obj = self._resolve_or_create_role(role_name)
            if role_obj:
                profile.role = role_obj
                profile.save()

        return instance


class CustomTimeSlotSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='slot_id', required=False)
    slot_id = serializers.CharField(required=False)

    class Meta:
        model = CustomTimeSlot
        fields = ['id', 'slot_id', 'label', 'created_at']

    def create(self, validated_data):
        import time
        slot_id = validated_data.get('slot_id') or validated_data.get('id') or f"slot-{int(time.time() * 1000)}"
        validated_data['slot_id'] = slot_id
        return super().create(validated_data)


class RoleSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='role_id', required=False)
    role_id = serializers.CharField(required=False)
    primary_desk = serializers.CharField(required=False, default='helpdesk')
    primaryDesk = serializers.CharField(source='primary_desk', required=False)
    allowed_desks = serializers.JSONField(required=False, default=list)
    allowedDesks = serializers.JSONField(source='allowed_desks', required=False)
    is_system_role = serializers.BooleanField(required=False, default=False)
    isSystemRole = serializers.BooleanField(source='is_system_role', required=False)
    created_at = serializers.DateTimeField(required=False)
    createdAt = serializers.DateTimeField(source='created_at', required=False)
    status = serializers.BooleanField(required=False, default=True)

    class Meta:
        model = Role
        fields = [
            'id', 'role_id', 'name', 'description', 'primary_desk', 'primaryDesk',
            'allowed_desks', 'allowedDesks', 'is_system_role', 'isSystemRole',
            'status', 'created_at', 'createdAt'
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, 'copy') else dict(data)

        # Handle status string 'Active' / 'Disabled' or bool
        status_val = data.get('status')
        if isinstance(status_val, str):
            if status_val.lower().strip() in ['active', 'true', '1', 'enabled']:
                data['status'] = True
            elif status_val.lower().strip() in ['disabled', 'inactive', 'false', '0']:
                data['status'] = False
        elif status_val is None:
            data['status'] = True

        # Handle camelCase vs snake_case field aliases
        if 'primaryDesk' in data and 'primary_desk' not in data:
            data['primary_desk'] = data['primaryDesk']
        if 'allowedDesks' in data and 'allowed_desks' not in data:
            data['allowed_desks'] = data['allowedDesks']
        if 'isSystemRole' in data and 'is_system_role' not in data:
            data['is_system_role'] = data['isSystemRole']

        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.role_id
        ret['role_id'] = instance.role_id
        ret['primaryDesk'] = instance.primary_desk
        ret['primary_desk'] = instance.primary_desk
        ret['allowedDesks'] = instance.allowed_desks or []
        ret['allowed_desks'] = instance.allowed_desks or []
        ret['isSystemRole'] = instance.is_system_role
        ret['is_system_role'] = instance.is_system_role
        ret['status'] = 'Active' if instance.status else 'Disabled'
        return ret

    def create(self, validated_data):
        import time
        role_id = validated_data.get('role_id') or validated_data.get('id') or f"role-{int(time.time() * 1000)}"
        validated_data['role_id'] = role_id
        return super().create(validated_data)
