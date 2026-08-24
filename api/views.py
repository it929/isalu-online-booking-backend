from rest_framework import viewsets, status, filters
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
import random

from .models import Department, Doctor, SpecialistSchedule, Booking, HmoCompany, SystemUser, CustomTimeSlot
from .serializers import (
    DepartmentSerializer,
    DoctorSerializer,
    SpecialistScheduleSerializer,
    BookingSerializer,
    HmoCompanySerializer,
    SystemUserSerializer,
    CustomTimeSlotSerializer
)

class StaffLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data or {}
        username_input = (data.get('username') or data.get('email') or data.get('user') or '').strip()
        password_input = (data.get('password') or data.get('pass') or '').strip()

        if not username_input or not password_input:
            return Response(
                {"error": "Please enter both Username/Email and Password."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Try Django Superuser / User Auth
        user = authenticate(username=username_input, password=password_input)
        if not user:
            from django.contrib.auth.models import User
            user_obj = User.objects.filter(email__iexact=username_input).first()
            if user_obj:
                user = authenticate(username=user_obj.username, password=password_input)

        if user and user.is_active:
            refresh = RefreshToken.for_user(user)
            user_display_name = user.get_full_name() or ("Dr. Chief Administrator" if user.username == "admin" else user.username.capitalize())
            return Response({
                "message": "Staff login successful",
                "user": {
                    "username": user.username,
                    "email": user.email or f"{user.username}@isaluhospitals.com",
                    "name": user_display_name,
                    "role": "Super Administrator" if user.is_superuser else "Hospital Staff",
                    "desk": "All Access" if user.is_superuser else "Hospital Desk",
                },
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                }
            }, status=status.HTTP_200_OK)

        # 2. Check SystemUser records with Strict Password Check
        valid_staff_passwords = ["admin123", "isalu2026", "admin", "password123"]

        disabled_sys_user = SystemUser.objects.filter(
            email__iexact=username_input, status="Disabled"
        ).first() or SystemUser.objects.filter(
            name__icontains=username_input, status="Disabled"
        ).first()

        if disabled_sys_user:
            return Response(
                {"error": f"Account Access Disabled: Staff account for '{disabled_sys_user.name}' has been disabled by the Administrator."},
                status=status.HTTP_403_FORBIDDEN
            )

        sys_user = SystemUser.objects.filter(
            email__iexact=username_input, status="Active"
        ).first() or SystemUser.objects.filter(
            name__icontains=username_input, status="Active"
        ).first()

        if sys_user:
            expected_password = sys_user.password if sys_user.password else "admin123"
            if password_input == expected_password:
                return Response({
                    "message": "System user authenticated successfully",
                    "user": {
                        "username": sys_user.email,
                        "email": sys_user.email,
                        "name": sys_user.name,
                        "role": sys_user.role,
                        "desk": sys_user.desk,
                    },
                    "tokens": {
                        "access": f"token-{sys_user.user_id}",
                        "refresh": f"refresh-{sys_user.user_id}",
                    }
                }, status=status.HTTP_200_OK)
            else:
                return Response(
                    {"error": "Invalid password. Please check your credentials and try again."},
                    status=status.HTTP_401_UNAUTHORIZED
                )

        return Response(
            {"error": "Invalid credentials. Please check your password and try again."},
            status=status.HTTP_401_UNAUTHORIZED
        )

class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    permission_classes = [AllowAny]
    lookup_field = 'dept_id'


class DoctorViewSet(viewsets.ModelViewSet):
    queryset = Doctor.objects.all()
    serializer_class = DoctorSerializer
    permission_classes = [AllowAny]
    lookup_field = 'doc_id'


class SpecialistScheduleViewSet(viewsets.ModelViewSet):
    queryset = SpecialistSchedule.objects.all()
    serializer_class = SpecialistScheduleSerializer
    permission_classes = [AllowAny]
    lookup_field = 'sched_id'


class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.all()
    serializer_class = BookingSerializer
    permission_classes = [AllowAny]
    lookup_field = 'ref_code'

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
    queryset = SystemUser.objects.all()
    serializer_class = SystemUserSerializer
    permission_classes = [AllowAny]
    lookup_field = 'user_id'


class CustomTimeSlotViewSet(viewsets.ModelViewSet):
    queryset = CustomTimeSlot.objects.all()
    serializer_class = CustomTimeSlotSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slot_id'
