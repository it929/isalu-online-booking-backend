from django.contrib import admin
from .models import Department, Doctor, SpecialistSchedule, Booking, HmoCompany, Company, CustomTimeSlot, Role, UserProfile, PaymentCheck, PaymentAttempt, PaymentEvent, Refund, WebhookEvent

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('dept_id', 'name', 'doctor_count')
    search_fields = ('dept_id', 'name')


@admin.register(Doctor)
class DoctorAdmin(admin.ModelAdmin):
    list_display = ('doc_id', 'full_name', 'name', 'acronym', 'specialty', 'department')
    search_fields = ('doc_id', 'full_name', 'name', 'acronym', 'specialty')
    list_filter = ('specialty', 'department')


@admin.register(SpecialistSchedule)
class SpecialistScheduleAdmin(admin.ModelAdmin):
    list_display = ('sched_id', 'doctor', 'room', 'shift_time', 'capacity', 'status')
    search_fields = ('sched_id', 'doctor__full_name', 'doctor__name', 'room')
    list_filter = ('status', 'doctor__department')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('ref_code', 'patient_name', 'patient_phone', 'doctor_name', 'date', 'time', 'payment_type', 'hmo_status', 'payment_status', 'status')
    search_fields = ('ref_code', 'patient_name', 'patient_phone', 'doctor_name', 'hmo_policy_code', 'hmo_auth_code')
    list_filter = ('status', 'payment_type', 'hmo_status', 'payment_status')


@admin.register(HmoCompany)
class HmoCompanyAdmin(admin.ModelAdmin):
    list_display = ('hmo_id', 'name', 'code', 'email', 'phone', 'contact_person', 'status')
    search_fields = ('name', 'code', 'email', 'contact_person')
    list_filter = ('status',)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('company_id', 'name', 'code', 'email', 'phone', 'contact_person', 'status')
    search_fields = ('name', 'code', 'email', 'contact_person')
    list_filter = ('status',)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('role_id', 'name', 'primary_desk', 'is_system_role', 'status')
    search_fields = ('name', 'description')
    list_filter = ('is_system_role', 'status')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role')
    search_fields = ('user__username', 'user__email', 'user__first_name')
    list_filter = ('role',)


@admin.register(CustomTimeSlot)
class CustomTimeSlotAdmin(admin.ModelAdmin):
    list_display = ('slot_id', 'label', 'created_at')
    search_fields = ('label',)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Audit records: viewable, never added, edited or deleted from the admin."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentCheck)
class PaymentCheckAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'reference', 'ref_code', 'patient_name', 'action', 'paystack_status', 'amount', 'verdict', 'checked_by')
    search_fields = ('reference', 'ref_code', 'patient_name', 'checked_by')
    list_filter = ('action', 'verdict')


@admin.register(PaymentEvent)
class PaymentEventAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'kind', 'reference', 'ref_code', 'patient_name', 'amount', 'source', 'actor')
    search_fields = ('reference', 'ref_code', 'patient_name', 'actor', 'note')
    list_filter = ('kind', 'source')


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'reference', 'booking_ref', 'amount_kobo', 'status', 'paid_at', 'last_checked_at')
    search_fields = ('reference', 'booking_ref')
    list_filter = ('status',)


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'reference', 'ref_code', 'patient_name', 'amount', 'reason', 'status', 'method', 'processed_by')
    search_fields = ('reference', 'ref_code', 'patient_name', 'paystack_refund_id')
    list_filter = ('status', 'reason', 'method')


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    list_display = ('received_at', 'event', 'reference', 'result')
    search_fields = ('reference',)
    list_filter = ('event',)
