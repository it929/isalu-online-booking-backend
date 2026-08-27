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
    if s in ('false', '0', 'inactive', 'disabled', 'off duty', 'cancelled', 'no'):
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
            'timeSlots': 'time_slots',
            'roomNumber': 'room_number',
            'acceptedPatientTypes': 'accepted_patient_types',
        }
        for camel, snake in mapping.items():
            if camel in data_copy:
                data_copy[snake] = data_copy[camel]
                data_copy.pop(camel, None)

        dept_val = data_copy.get('department_id') or data_copy.get('departmentId') or data_copy.get('department')
        if dept_val:
            dept_str = str(dept_val).strip()
            dept_obj = Department.objects.filter(dept_id__iexact=dept_str).first()
            if dept_obj:
                data_copy['department'] = dept_obj.dept_id
            else:
                data_copy['department'] = None

        if 'status' in data_copy:
            data_copy['status'] = parse_bool_status(data_copy['status'])

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.doc_id
        ret['doc_id'] = instance.doc_id
        ret['fullName'] = instance.full_name or instance.name
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
        ret['timeSlots'] = instance.time_slots or []
        ret['roomNumber'] = instance.room_number or ''
        types = instance.accepted_patient_types
        if not types or len(types) == 0:
            types = ["Private Self-Pay", "HMO Insurance"]
        ret['acceptedPatientTypes'] = types
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
                data_copy.pop(camel, None)

        doc_val = data_copy.get('doctor_id') or data_copy.get('doctorId') or data_copy.get('doctor')
        if doc_val:
            doc_str = str(doc_val).strip()
            doc_obj = Doctor.objects.filter(doc_id__iexact=doc_str).first()
            if doc_obj:
                data_copy['doctor'] = doc_obj.doc_id
                if not data_copy.get('doctor_name'):
                    data_copy['doctor_name'] = doc_obj.full_name or doc_obj.name
                if not data_copy.get('specialty'):
                    data_copy['specialty'] = doc_obj.specialty
            else:
                data_copy['doctor'] = None

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        doc_id_val = instance.doctor.doc_id if instance.doctor else (getattr(instance, 'doctor_id', None) or '')
        doc_name_val = instance.doctor_name or (instance.doctor.full_name if instance.doctor else '')
        ret['id'] = instance.sched_id
        ret['doctorId'] = doc_id_val
        ret['doctor_id'] = doc_id_val
        ret['doctorName'] = doc_name_val
        ret['dutyDays'] = instance.duty_days or []
        ret['dayConfigs'] = instance.day_configs or {}
        ret['shiftTime'] = instance.shift_time
        ret['totalWeeklyCapacity'] = instance.total_weekly_capacity or instance.capacity
        return ret


class BookingSerializer(serializers.ModelSerializer):
    refCode = serializers.CharField(source='ref_code', required=False)
    doctorId = serializers.CharField(source='doctor_id', required=False)
    doctorName = serializers.CharField(source='doctor_name', required=False)
    doctorSpecialty = serializers.CharField(source='doctor_specialty', required=False)
    patientName = serializers.CharField(source='patient_name', required=False)
    patientPhone = serializers.CharField(source='patient_phone', required=False)
    patientEmail = serializers.EmailField(source='patient_email', required=False, allow_blank=True)
    paymentType = serializers.CharField(source='payment_type', required=False)
    hmoName = serializers.CharField(source='hmo_name', required=False, allow_blank=True)
    hmoPolicyCode = serializers.CharField(source='hmo_policy_code', required=False, allow_blank=True)
    hmoAuthCode = serializers.CharField(source='hmo_auth_code', required=False, allow_blank=True)
    referralDocName = serializers.CharField(source='referral_doc_name', required=False, allow_blank=True)
    hmoStatus = serializers.CharField(source='hmo_status', required=False, allow_blank=True)
    paymentStatus = serializers.CharField(source='payment_status', required=False, allow_blank=True)
    paymentMethod = serializers.CharField(source='payment_method', required=False, allow_blank=True)
    invoiceRef = serializers.CharField(source='invoice_ref', required=False, allow_blank=True)
    isActive = serializers.BooleanField(source='is_active', required=False, default=True)
    deleteReason = serializers.CharField(source='delete_reason', required=False, allow_blank=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Booking
        fields = [
            'ref_code', 'refCode', 'doctor_id', 'doctorId', 'doctor_name', 'doctorName',
            'doctor_specialty', 'doctorSpecialty', 'date', 'time', 'patient_name', 'patientName',
            'patient_phone', 'patientPhone', 'patient_email', 'patientEmail', 'reason',
            'payment_type', 'paymentType', 'hmo_name', 'hmoName', 'hmo_policy_code', 'hmoPolicyCode',
            'hmo_auth_code', 'hmoAuthCode', 'referral_doc_name', 'referralDocName', 'hmo_status',
            'hmoStatus', 'payment_status', 'paymentStatus', 'payment_method', 'paymentMethod',
            'invoice_ref', 'invoiceRef', 'status', 'is_active', 'isActive', 'delete_reason', 'deleteReason',
            'created_at', 'createdAt'
        ]

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
                if snake not in data_copy or not data_copy[snake]:
                    data_copy[snake] = data_copy[camel]
                data_copy.pop(camel, None)

        return super().to_internal_value(data_copy)

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
