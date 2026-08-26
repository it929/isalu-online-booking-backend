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


class StaffLoginView(APIView):
    permission_classes = [AllowAny]

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
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = 'dept_id'


class DoctorViewSet(viewsets.ModelViewSet):
    serializer_class = DoctorSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = 'doc_id'

    def get_queryset(self):
        queryset = Doctor.objects.all().select_related('department')
        dept_param = self.request.query_params.get('department') or self.request.query_params.get('department_id') or self.request.query_params.get('dept_id')
        if dept_param and dept_param != 'all':
            dept_clean = dept_param.strip().lower()
            queryset = queryset.filter(department__dept_id__iexact=dept_clean)
        return queryset


class SpecialistScheduleViewSet(viewsets.ModelViewSet):
    queryset = SpecialistSchedule.objects.all()
    serializer_class = SpecialistScheduleSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = 'sched_id'


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    lookup_field = 'ref_code'

    def get_queryset(self):
        include_disabled = self.request.query_params.get('include_disabled') == 'true'
        if include_disabled:
            return Booking.objects.all()
        return Booking.objects.filter(is_active=True).exclude(status="Disabled")

    def get_permissions(self):
        return [AllowAny()]

    def destroy(self, request, *args, **kwargs):
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
    permission_classes = [IsAuthenticatedOrReadOnly]
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
    queryset = User.objects.all().order_by('id')
    serializer_class = SystemUserSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'id'

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        val = str(self.kwargs[lookup_url_kwarg])
        if val.startswith('usr-'):
            val = val.replace('usr-', '')
        return User.objects.get(id=val)


class CustomTimeSlotViewSet(viewsets.ModelViewSet):
    queryset = CustomTimeSlot.objects.all()
    serializer_class = CustomTimeSlotSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = 'slot_id'


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_field = 'role_id'

