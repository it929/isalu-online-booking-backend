from rest_framework import serializers
from .models import Department, Doctor, SpecialistSchedule, Booking, HmoCompany, SystemUser, CustomTimeSlot

class DepartmentSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='dept_id', read_only=True)
    
    class Meta:
        model = Department
        fields = '__all__'

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.dept_id
        ret['status'] = getattr(instance, 'status', None) or 'Active'
        ret['location'] = getattr(instance, 'location', None) or 'Main Building'
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
            'departmentId': 'department_id',
            'availableDays': 'available_days',
            'timeSlots': 'time_slots',
            'roomNumber': 'room_number',
            'acceptedPatientTypes': 'accepted_patient_types',
        }
        for camel, snake in mapping.items():
            if camel in data_copy:
                data_copy[snake] = data_copy[camel]
                data_copy.pop(camel, None)

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.doc_id
        ret['fullName'] = instance.full_name or instance.name
        ret['departmentId'] = instance.department_id
        ret['availableDays'] = instance.available_days or []
        ret['timeSlots'] = instance.time_slots or []
        ret['roomNumber'] = instance.room_number or ''
        ret['acceptedPatientTypes'] = instance.accepted_patient_types or ["Private Self-Pay", "HMO Insurance"]
        ret['status'] = instance.status or 'Active'
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
            'doctorId': 'doctor_id',
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

        return super().to_internal_value(data_copy)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['id'] = instance.sched_id
        ret['doctorId'] = instance.doctor_id
        ret['doctorName'] = instance.doctor_name
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
    id = serializers.CharField(source='user_id', required=False)
    user_id = serializers.CharField(required=False)
    password = serializers.CharField(required=False, default='admin123')
    lastActive = serializers.CharField(source='last_active', required=False)

    class Meta:
        model = SystemUser
        fields = [
            'id', 'user_id', 'name', 'email', 'password', 'role', 'desk', 'status', 'last_active', 'lastActive'
        ]

    def create(self, validated_data):
        import time
        user_id = validated_data.get('user_id') or validated_data.get('id') or f"usr-{int(time.time() * 1000)}"
        validated_data['user_id'] = user_id
        return super().create(validated_data)


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
