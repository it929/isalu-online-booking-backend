from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    StaffLoginView,
    CustomTokenRefreshView,
    HospitalEventStreamView,
    DepartmentViewSet,
    DoctorViewSet,
    SpecialistScheduleViewSet,
    BookingViewSet,
    HmoCompanyViewSet,
    SystemUserViewSet,
    CustomTimeSlotViewSet,
    RoleViewSet,
    AiReportView,
    AppSettingViewSet
)

router = DefaultRouter()
router.register(r'departments', DepartmentViewSet, basename='department')
router.register(r'doctors', DoctorViewSet, basename='doctor')
router.register(r'schedules', SpecialistScheduleViewSet, basename='schedule')
router.register(r'bookings', BookingViewSet, basename='booking')
router.register(r'hmo-companies', HmoCompanyViewSet, basename='hmocompany')
router.register(r'users', SystemUserViewSet, basename='systemuser')
router.register(r'time-slots', CustomTimeSlotViewSet, basename='timeslot')
router.register(r'roles', RoleViewSet, basename='role')
router.register(r'settings', AppSettingViewSet, basename='setting')

urlpatterns = [
    path('auth/staff-login/', StaffLoginView.as_view(), name='staff-login'),
    path('auth/token-refresh/', CustomTokenRefreshView.as_view(), name='token-refresh'),
    path('stream/events/', HospitalEventStreamView.as_view(), name='event-stream'),
    path('analytics/ai-report/', AiReportView.as_view(), name='ai-report'),
    path('', include(router.urls)),
]
