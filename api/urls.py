from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    StaffLoginView,
    StaffProfileView,
    CustomTokenRefreshView,
    HospitalEventStreamView,
    DepartmentViewSet,
    DoctorViewSet,
    SpecialistScheduleViewSet,
    BookingViewSet,
    HmoCompanyViewSet,
    CompanyViewSet,
    SystemUserViewSet,
    CustomTimeSlotViewSet,
    RoleViewSet,
    AiReportView,
    AppSettingViewSet,
    ClinicAnalyticsViewSet,
    ScheduleExceptionViewSet,
)

from .payment_views import ReconcileLinkView, ReconcileResolveView, RescheduleQuoteView, RescheduleStartView, RescheduleVerifyView, PaymentConfigView, PaystackWebhookView, PaymentReconcileView, PaymentReportView, ReconcileDailyView, ReconcileLedgerView, ReconcileRefundView, ReconcileSweepView, StartPaymentView, VerifyPaymentView

router = DefaultRouter()
router.register(r'departments', DepartmentViewSet, basename='department')
router.register(r'doctors', DoctorViewSet, basename='doctor')
router.register(r'schedules', SpecialistScheduleViewSet, basename='schedule')
router.register(r'bookings', BookingViewSet, basename='booking')
router.register(r'hmo-companies', HmoCompanyViewSet, basename='hmocompany')
router.register(r'companies', CompanyViewSet, basename='company')
router.register(r'users', SystemUserViewSet, basename='systemuser')
router.register(r'time-slots', CustomTimeSlotViewSet, basename='timeslot')
router.register(r'roles', RoleViewSet, basename='role')
router.register(r'settings', AppSettingViewSet, basename='setting')
router.register(r'clinic-analytics', ClinicAnalyticsViewSet, basename='clinic-analytics')
router.register(r'schedule-exceptions', ScheduleExceptionViewSet, basename='schedule-exception')

from .module_access import ModuleAccessRoleView, ModuleAccessUserView, ModuleAccessView
from .walk_in import WalkInClinicDaysView, WalkInRegisterView, WalkInVisitTypeView
from .telemedicine_views import (
    OnlineSessionActionView,
    OnlineSessionDetailView,
    OnlineSessionListView,
    PatientJoinView,
    PublicOnlineSlotsView,
)

urlpatterns = [
    path('auth/staff-login/', StaffLoginView.as_view(), name='staff-login'),
    path('auth/me/', StaffProfileView.as_view(), name='staff-me'),
    path('auth/token-refresh/', CustomTokenRefreshView.as_view(), name='token-refresh'),
    path('stream/events/', HospitalEventStreamView.as_view(), name='event-stream'),
    path('analytics/ai-report/', AiReportView.as_view(), name='ai-report'),
    path('payments/config/', PaymentConfigView.as_view(), name='payment-config'),
    path('payments/start/', StartPaymentView.as_view(), name='payment-start'),
    path('payments/verify/', VerifyPaymentView.as_view(), name='payment-verify'),
    path('payments/paystack-webhook/', PaystackWebhookView.as_view(), name='paystack-webhook'),
    path('payments/report/', PaymentReportView.as_view(), name='payment-report'),
    path('payments/reconcile/', PaymentReconcileView.as_view(), name='payment-reconcile'),
    path('payments/reschedule/quote/', RescheduleQuoteView.as_view(), name='reschedule-quote'),
    path('payments/reschedule/start/', RescheduleStartView.as_view(), name='reschedule-start'),
    path('payments/reschedule/verify/', RescheduleVerifyView.as_view(), name='reschedule-verify'),
    path('payments/reconcile/refunds/', ReconcileRefundView.as_view(), name='payment-reconcile-refunds'),
    path('payments/reconcile/link/', ReconcileLinkView.as_view(), name='payment-reconcile-link'),
    path('payments/reconcile/resolve/', ReconcileResolveView.as_view(), name='payment-reconcile-resolve'),
    path('payments/reconcile/daily/', ReconcileDailyView.as_view(), name='payment-reconcile-daily'),
    path('payments/reconcile/ledger/', ReconcileLedgerView.as_view(), name='payment-reconcile-ledger'),
    path('payments/reconcile/sweep/', ReconcileSweepView.as_view(), name='payment-reconcile-sweep'),
    # Online (video) consultations
    path('online-sessions/', OnlineSessionListView.as_view(), name='online-sessions'),
    path('online-sessions/<str:session_id>/', OnlineSessionDetailView.as_view(), name='online-session'),
    path('online-sessions/<str:session_id>/<str:action>/', OnlineSessionActionView.as_view(), name='online-session-action'),
    path('telemedicine/slots/', PublicOnlineSlotsView.as_view(), name='telemedicine-slots'),
    # Module Access (administrators only)
    path('module-access/', ModuleAccessView.as_view(), name='module-access'),
    path('module-access/roles/<str:role_id>/', ModuleAccessRoleView.as_view(), name='module-access-role'),
    path('module-access/users/<int:user_id>/', ModuleAccessUserView.as_view(), name='module-access-user'),
    # Helpdesk walk-in registration
    path('walk-in/', WalkInRegisterView.as_view(), name='walk-in-register'),
    path('walk-in/clinic-days/', WalkInClinicDaysView.as_view(), name='walk-in-days'),
    path('walk-in/visit-type/', WalkInVisitTypeView.as_view(), name='walk-in-visit-type'),
    path('telemedicine/join/<str:token>/', PatientJoinView.as_view(), name='telemedicine-join'),
    path('', include(router.urls)),
]
