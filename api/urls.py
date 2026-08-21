from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    StaffLoginView,
    DepartmentViewSet,
    DoctorViewSet,
    SpecialistScheduleViewSet,
    BookingViewSet,
    HmoCompanyViewSet,
    SystemUserViewSet,
    CustomTimeSlotViewSet
)

router = DefaultRouter()
router.register(r'departments', DepartmentViewSet, basename='department')
router.register(r'doctors', DoctorViewSet, basename='doctor')
router.register(r'schedules', SpecialistScheduleViewSet, basename='schedule')
router.register(r'bookings', BookingViewSet, basename='booking')
router.register(r'hmo-companies', HmoCompanyViewSet, basename='hmocompany')
router.register(r'users', SystemUserViewSet, basename='systemuser')
router.register(r'time-slots', CustomTimeSlotViewSet, basename='timeslot')

urlpatterns = [
    path('auth/staff-login/', StaffLoginView.as_view(), name='staff-login'),
    path('', include(router.urls)),
]
