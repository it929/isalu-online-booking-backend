from rest_framework import viewsets, status, filters
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAuthenticatedOrReadOnly
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
import random

from .models import Department, Doctor, SpecialistSchedule, Booking, HmoCompany, CustomTimeSlot, Role, UserProfile
from .serializers import (
    DepartmentSerializer,
    DoctorSerializer,
    SpecialistScheduleSerializer,
    BookingSerializer,
    HmoCompanySerializer,
    SystemUserSerializer,
    CustomTimeSlotSerializer,
    RoleSerializer
)

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator


def is_staff_request(request):
    if hasattr(request, 'user') and request.user and request.user.is_authenticated:
        return True

    auth_header = request.headers.get('Authorization') or request.META.get('HTTP_AUTHORIZATION', '')
    if auth_header and (auth_header.startswith('Bearer ') or auth_header.startswith('Token ')):
        token_str = auth_header.split(' ')[1].strip()
        if token_str and token_str != 'null' and token_str != 'undefined':
            try:
                from rest_framework_simplejwt.tokens import AccessToken
                validated_token = AccessToken(token_str)
                user_id = validated_token.get('user_id')
                if user_id:
                    user = User.objects.filter(id=user_id).first()
                    if user and user.is_active:
                        request.user = user
                        return True
            except Exception:
                pass
    return False


@method_decorator(csrf_exempt, name='dispatch')
class StaffLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        data = request.data or {}
        username_input = (data.get('username') or data.get('email') or data.get('user') or '').strip()
        password_input = (data.get('password') or data.get('pass') or '').strip()

        if not username_input or not password_input:
            return Response(
                {"error": "Please enter both Email/Username and Password."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if user account is disabled first
        user_obj = (
            User.objects.filter(email__iexact=username_input).first()
            or User.objects.filter(username__iexact=username_input).first()
        )
        if user_obj and not user_obj.is_active:
            user_name = user_obj.first_name or user_obj.username
            return Response(
                {"error": f"Account Access Disabled: Staff account for '{user_name}' has been disabled by the Administrator."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Try authenticating
        user = authenticate(username=username_input, password=password_input)
        if not user and user_obj:
            user = authenticate(username=user_obj.username, password=password_input)

        if user and user.is_active:
            refresh = RefreshToken.for_user(user)
            role_name = "Super Administrator" if user.is_superuser else "Helpdesk Officer"
            desk_name = "All Access" if user.is_superuser else "Helpdesk Reception"

            if hasattr(user, 'profile') and user.profile and user.profile.role:
                role_name = user.profile.role.name
                desk_name = user.profile.role.primary_desk

            user_display_name = user.first_name or user.username
            return Response({
                "message": "Staff login successful",
                "user": {
                    "username": user.username,
                    "email": user.email or f"{user.username}@isaluhospitals.com",
                    "name": user_display_name,
                    "role": role_name,
                    "desk": desk_name,
                },
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                }
            }, status=status.HTTP_200_OK)

        return Response(
            {"error": "Invalid email or password. Please check your credentials and try again."},
            status=status.HTTP_401_UNAUTHORIZED
        )

class DepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    lookup_field = 'dept_id'

    def get_queryset(self):
        queryset = Department.objects.all()
        if self.action in ['retrieve', 'update', 'partial_update', 'destroy', 'restore']:
            return queryset

        include_disabled = self.request.query_params.get('include_disabled') == 'true'
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if status_param:
            st = str(status_param).strip().lower()
            if st in ('active', 'true', '1'):
                queryset = queryset.filter(status=True)
            elif st in ('disabled', 'maintenance', 'under maintenance', 'inactive', 'false', '0'):
                queryset = queryset.filter(status=False)
        elif not include_disabled:
            queryset = queryset.filter(status=True)

        if search_param:
            from django.db.models import Q
            q = str(search_param).strip()
            queryset = queryset.filter(
                Q(name__icontains=q) | Q(dept_id__icontains=q) | Q(description__icontains=q) | Q(location__icontains=q)
            )

        return queryset

    def destroy(self, request, *args, **kwargs):
        dept = self.get_object()
        dept.status = False
        dept.save()
        return Response(
            {"message": f"Department '{dept.name}' disabled successfully.", "data": DepartmentSerializer(dept).data},
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'], url_path='restore')
    def restore(self, request, *args, **kwargs):
        dept = self.get_object()
        dept.status = True
        dept.save()
        return Response(
            {"message": f"Department '{dept.name}' restored successfully.", "data": DepartmentSerializer(dept).data},
            status=status.HTTP_200_OK
        )


class DoctorViewSet(viewsets.ModelViewSet):
    serializer_class = DoctorSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    lookup_field = 'doc_id'

    def get_queryset(self):
        queryset = Doctor.objects.all().select_related('department')
        dept_param = self.request.query_params.get('department') or self.request.query_params.get('department_id') or self.request.query_params.get('dept_id')
        if dept_param and dept_param != 'all':
            dept_clean = dept_param.strip().lower()
            queryset = queryset.filter(department__dept_id__iexact=dept_clean)
        return queryset

    def create(self, request, *args, **kwargs):
        import time
        from .models import Doctor, Department

        data = request.data
        doc_id = data.get('doc_id') or data.get('id') or f"doc-{int(time.time() * 1000)}"
        name = data.get('name') or data.get('fullName') or data.get('full_name') or 'Specialist Doctor'
        full_name = data.get('fullName') or data.get('full_name') or name
        acronym = data.get('acronym') or name
        specialty = data.get('specialty') or 'Specialist Consultation'
        qualification = data.get('qualification') or data.get('qualifications') or 'MBBS, FWACS'
        room = data.get('roomNumber') or data.get('room_number') or data.get('room') or 'Consultation Suite'
        available_days = data.get('availableDays') or data.get('available_days') or data.get('availability') or ["Monday", "Wednesday", "Friday"]
        time_slots = data.get('timeSlots') or data.get('time_slots') or ["08:00 AM – 02:00 PM"]
        accepted_types = data.get('acceptedPatientTypes') or data.get('accepted_patient_types') or ["Private Self-Pay", "HMO Insurance"]
        dept_id = data.get('departmentId') or data.get('department_id') or data.get('department')

        dept_obj = None
        if dept_id:
            dept_obj = Department.objects.filter(dept_id__iexact=str(dept_id).strip()).first()
        if not dept_obj and specialty:
            dept_obj = Department.objects.filter(name__icontains=specialty.strip()).first()

        doc_obj = Doctor.objects.filter(doc_id=doc_id).first()
        if not doc_obj:
            doc_obj = Doctor(doc_id=doc_id)

        doc_obj.name = name
        doc_obj.full_name = full_name
        doc_obj.acronym = acronym
        doc_obj.specialty = specialty
        if dept_obj:
            doc_obj.department = dept_obj
        doc_obj.qualification = qualification
        doc_obj.qualifications = qualification
        doc_obj.room_number = room
        doc_obj.available_days = available_days
        doc_obj.time_slots = time_slots
        doc_obj.accepted_patient_types = accepted_types
        doc_obj.status = True
        doc_obj.save()

        serializer = self.get_serializer(doc_obj)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class SpecialistScheduleViewSet(viewsets.ModelViewSet):
    queryset = SpecialistSchedule.objects.all().order_by('-sched_id')
    serializer_class = SpecialistScheduleSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    lookup_field = 'sched_id'

    def create(self, request, *args, **kwargs):
        import time
        from .models import Doctor, SpecialistSchedule

        data = request.data
        sched_id = data.get('sched_id') or data.get('id') or f"sched-{int(time.time() * 1000)}"
        doc_id = data.get('doctor_id') or data.get('doctorId') or data.get('doctor')
        doc_name = data.get('doctor_name') or data.get('doctorName') or 'Specialist Doctor'
        specialty = data.get('specialty') or 'Specialist Consultation'
        room = data.get('room') or 'Consultation Suite 4B'
        duty_days = data.get('duty_days') or data.get('dutyDays') or []
        day_configs = data.get('day_configs') or data.get('dayConfigs') or {}
        shift_time = data.get('shift_time') or data.get('shiftTime') or '08:00 AM – 02:00 PM'
        capacity = data.get('capacity') or 15
        total_capacity = data.get('total_weekly_capacity') or data.get('totalWeeklyCapacity') or capacity

        # 1. Ensure Doctor object exists in Doctor DB table
        doc_obj = None
        if doc_id:
            doc_obj = Doctor.objects.filter(doc_id__iexact=str(doc_id).strip()).first()
        if not doc_obj and doc_name:
            doc_obj = Doctor.objects.filter(name__iexact=str(doc_name).strip()).first() or Doctor.objects.filter(full_name__iexact=str(doc_name).strip()).first()

        if not doc_obj:
            new_doc_id = doc_id or f"doc-{int(time.time() * 1000)}"
            doc_obj = Doctor.objects.create(
                doc_id=new_doc_id,
                name=doc_name,
                full_name=doc_name,
                acronym=doc_name,
                specialty=specialty,
                qualification='MBBS, FWACS',
                room_number=room,
                available_days=duty_days,
                status=True
            )
        else:
            doc_obj.available_days = duty_days
            doc_obj.room_number = room
            doc_obj.save()

        # 2. Save / Update SpecialistSchedule in database
        sched_obj = SpecialistSchedule.objects.filter(sched_id=sched_id).first()
        if not sched_obj:
            sched_obj = SpecialistSchedule(sched_id=sched_id)

        sched_obj.doctor = doc_obj
        sched_obj.doctor_name = doc_name
        sched_obj.specialty = specialty
        sched_obj.room = room
        sched_obj.duty_days = duty_days
        sched_obj.day_configs = day_configs
        sched_obj.shift_time = shift_time
        sched_obj.capacity = capacity
        sched_obj.total_weekly_capacity = total_capacity
        sched_obj.status = True
        sched_obj.save()

        serializer = self.get_serializer(sched_obj)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [AllowAny]
    lookup_field = 'ref_code'

    def get_queryset(self):
        include_disabled = self.request.query_params.get('include_disabled') == 'true'
        if include_disabled:
            return Booking.objects.all()
        return Booking.objects.filter(is_active=True).exclude(status="Disabled")

    def list(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {"detail": "Authentication required. Access to patient booking registry is restricted to authorized hospital staff."},
                status=status.HTTP_401_UNAUTHORIZED
            )
        return super().list(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {"detail": "Authentication required. Only authorized hospital staff can delete or disable appointment records."},
                status=status.HTTP_403_FORBIDDEN
            )
        booking = self.get_object()
        reason = request.data.get('reason') or request.data.get('delete_reason') or request.data.get('deleteReason') or "Disabled by Administrator"
        booking.is_active = False
        booking.status = "Disabled"
        booking.delete_reason = reason
        booking.save()
        return Response(
            {"message": f"Booking {booking.ref_code} disabled successfully.", "data": BookingSerializer(booking).data},
            status=status.HTTP_200_OK
        )

    def partial_update(self, request, *args, **kwargs):
        booking = self.get_object()
        new_status = request.data.get('status')
        if new_status == "Completed" and booking.payment_status == "Pending":
            return Response(
                {"error": f"Payment Clearance Required: Ticket {booking.ref_code} cannot be marked as Completed while payment status is Pending."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().partial_update(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        booking = self.get_object()
        new_status = request.data.get('status')
        if new_status == "Completed" and booking.payment_status == "Pending":
            return Response(
                {"error": f"Payment Clearance Required: Ticket {booking.ref_code} cannot be marked as Completed while payment status is Pending."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().update(request, *args, **kwargs)

    @action(detail=False, methods=['get'], url_path='summary')
    def summary(self, request):
        total = Booking.objects.count()
        checked_in = Booking.objects.filter(status="Checked In").count()
        pending_hmo = Booking.objects.filter(payment_type="HMO Insurance").exclude(hmo_status="Approved").count()
        pending_cash = Booking.objects.filter(payment_type="Private Self-Pay").exclude(payment_status="Cleared").count()
        
        return Response({
            "totalBookings": total,
            "checkedInCount": checked_in,
            "pendingHmoCount": pending_hmo,
            "pendingCashCount": pending_cash,
        })

    @action(detail=False, methods=['get'], url_path='disabled')
    def disabled_bookings(self, request):
        from django.db.models import Q
        queryset = Booking.objects.filter(Q(is_active=False) | Q(status="Disabled"))
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='restore')
    def restore_booking(self, request, ref_code=None):
        booking = Booking.objects.filter(ref_code=ref_code).first()
        if not booking:
            return Response({"error": "Booking record not found."}, status=status.HTTP_404_NOT_FOUND)
        booking.is_active = True
        booking.status = "Booked"
        booking.delete_reason = ""
        booking.save()
        return Response({"message": f"Booking {booking.ref_code} restored successfully.", "data": BookingSerializer(booking).data})

    @action(detail=True, methods=['post', 'patch'], url_path='reroute-cashdesk')
    def reroute_cashdesk(self, request, ref_code=None):
        booking = Booking.objects.filter(ref_code=ref_code).first()
        if not booking:
            booking = self.get_object()
        if not booking:
            return Response({"error": f"Booking ticket {ref_code} not found."}, status=status.HTTP_404_NOT_FOUND)

        remark = request.data.get('remark') or request.data.get('delete_reason') or request.data.get('hmoRemark') or request.data.get('hmo_status') or "Passed from HMO to Cashdesk"

        booking.payment_type = "Private Self-Pay"
        booking.hmo_name = "N/A"
        booking.hmo_status = f"Re-routed to Cashdesk (Self-Pay): {remark}"
        booking.payment_status = "Pending"
        booking.delete_reason = f"Re-routed from HMO to Cashdesk: {remark}"
        booking.save()

        return Response({
            "message": f"Ticket {booking.ref_code} re-routed to Cashdesk as Private Self-Pay.",
            "data": BookingSerializer(booking).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='check-in')
    def check_in(self, request, ref_code=None):
        booking = self.get_object()

        if booking.payment_type == "HMO Insurance" and booking.hmo_status != "Approved":
            return Response(
                {"error": f"HMO Approval Required: Cannot check in ticket {booking.ref_code} while HMO status is {booking.hmo_status or 'Awaiting Approval'}. Route patient to HMO Desk first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if booking.payment_status == "Pending":
            return Response(
                {"error": f"Payment Clearance Required: Cannot check in ticket {booking.ref_code} while payment is Pending. Route patient to Cashdesk first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        booking.status = "Checked In"
        booking.save()
        return Response({"message": f"Patient {booking.patient_name} checked in successfully.", "data": BookingSerializer(booking).data})

    @action(detail=True, methods=['post'], url_path='approve-hmo')
    def approve_hmo(self, request, ref_code=None):
        booking = self.get_object()
        policy = request.data.get('policyCode') or booking.hmo_policy_code or f"POL-{random.randint(100000, 999999)}"
        auth = request.data.get('authCode') or booking.hmo_auth_code or f"AUTH-{random.randint(1000, 9999)}"

        booking.hmo_policy_code = policy
        booking.hmo_auth_code = auth
        booking.hmo_status = "Approved"
        booking.payment_status = "Cleared"
        booking.save()

        return Response({
            "message": f"Pre-Authorization cleared for ticket {booking.ref_code}.",
            "authCode": auth,
            "data": BookingSerializer(booking).data
        })

    @action(detail=True, methods=['post'], url_path='pay-cashdesk')
    def pay_cashdesk(self, request, ref_code=None):
        booking = self.get_object()
        method = request.data.get('paymentMethod', 'POS Card Terminal')
        invoice = f"INV-{random.randint(100000, 999999)}"

        booking.payment_status = "Cleared"
        booking.payment_method = method
        booking.invoice_ref = invoice
        booking.save()

        return Response({
            "message": f"Cashdesk payment cleared via {method}.",
            "invoiceRef": invoice,
            "data": BookingSerializer(booking).data
        })


class HmoCompanyViewSet(viewsets.ModelViewSet):
    queryset = HmoCompany.objects.all()
    serializer_class = HmoCompanySerializer
    permission_classes = [AllowAny]
    lookup_field = 'hmo_id'

    def create(self, request, *args, **kwargs):
        hmo_id = request.data.get('hmo_id') or request.data.get('id')
        name = request.data.get('name')
        if hmo_id:
            existing = HmoCompany.objects.filter(hmo_id=hmo_id).first()
            if existing:
                serializer = self.get_serializer(existing, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        if name:
            existing = HmoCompany.objects.filter(name__iexact=str(name).strip()).first()
            if existing:
                serializer = self.get_serializer(existing, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)


class SystemUserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by('-date_joined', '-id')
    serializer_class = SystemUserSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    lookup_field = 'id'

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        val = str(self.kwargs[lookup_url_kwarg])
        if val.startswith('usr-'):
            val = val.replace('usr-', '')
        return User.objects.get(id=val)

    def list(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {"detail": "Authentication required. Access to system staff user directory is restricted to authorized staff administrators."},
                status=status.HTTP_401_UNAUTHORIZED
            )
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {"detail": "Authentication required. Only authorized staff administrators can create system user accounts."},
                status=status.HTTP_403_FORBIDDEN
            )

        import time
        from django.contrib.auth.models import User
        from .models import UserProfile, Role

        data = request.data
        email = (data.get('email') or '').strip().lower()
        raw_password = data.get('password') or 'admin123'
        name = (data.get('name') or data.get('first_name') or (email.split('@')[0] if email else 'Staff User')).strip()
        role_name = data.get('role') or 'Helpdesk Officer'
        status_input = data.get('status', 'Active')

        username = email if email else f"user_{int(time.time() * 1000)}"

        # 1. Save / Update User in auth_user table
        user = User.objects.filter(email__iexact=email).first() if email else None
        if not user:
            user = User.objects.filter(username__iexact=username).first()

        if user:
            user.first_name = name
            if raw_password:
                user.set_password(raw_password)
            user.is_staff = True
            user.is_active = (status_input != 'Disabled' and status_input != 'false' and status_input != False)
            user.save()
        else:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=raw_password,
                first_name=name,
                is_staff=True,
                is_active=(status_input != 'Disabled' and status_input != 'false' and status_input != False)
            )

        # 2. Save / Update UserProfile in api_userprofile table
        role_obj = (
            Role.objects.filter(name__iexact=role_name).first()
            or Role.objects.filter(role_id__iexact=role_name).first()
            or Role.objects.filter(name__icontains=role_name).first()
        )
        if not role_obj:
            role_id_clean = role_name.lower().replace(' ', '-')
            role_obj, _ = Role.objects.get_or_create(
                role_id=role_id_clean,
                defaults={'name': role_name, 'primary_desk': 'helpdesk', 'status': True}
            )

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = role_obj
        profile.save()

        # 3. Return serialized data
        serializer = self.get_serializer(user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        if not is_staff_request(request):
            return Response(
                {"detail": "Authentication required. Only authorized staff administrators can deactivate system user accounts."},
                status=status.HTTP_403_FORBIDDEN
            )
        user = self.get_object()
        user.is_active = False
        user.save()
        return Response({"message": f"User {user.username} deactivated successfully."}, status=status.HTTP_200_OK)


class CustomTimeSlotViewSet(viewsets.ModelViewSet):
    queryset = CustomTimeSlot.objects.all()
    serializer_class = CustomTimeSlotSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slot_id'


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [AllowAny]
    lookup_field = 'role_id'

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        val = str(self.kwargs[lookup_url_kwarg])
        role = Role.objects.filter(role_id=val).first()
        if not role:
            role = Role.objects.filter(name__iexact=val).first()
        if not role:
            role = super().get_object()
        return role

    def create(self, request, *args, **kwargs):
        role_id = request.data.get('role_id') or request.data.get('id')
        name = request.data.get('name')
        if role_id:
            existing = Role.objects.filter(role_id=role_id).first()
            if existing:
                serializer = self.get_serializer(existing, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        if name:
            existing = Role.objects.filter(name__iexact=str(name).strip()).first()
            if existing:
                serializer = self.get_serializer(existing, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)


