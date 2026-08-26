from django.contrib import admin
from .models import Department, Doctor, SpecialistSchedule, Booking, HmoCompany, CustomTimeSlot, Role, UserProfile

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('dept_id', 'name', 'doctor_count')
    search_fields = ('dept_id', 'name')


@admin.register(Doctor)
class DoctorAdmin(admin.ModelAdmin):
    list_display = ('doc_id', 'full_name', 'name', 'acronym', 'specialty', 'department', 'room_number')
    search_fields = ('doc_id', 'full_name', 'name', 'acronym', 'specialty')
    list_filter = ('specialty', 'department')


@admin.register(SpecialistSchedule)
class SpecialistScheduleAdmin(admin.ModelAdmin):
    list_display = ('sched_id', 'doctor_name', 'specialty', 'room', 'shift_time', 'capacity', 'status')
    search_fields = ('sched_id', 'doctor_name', 'specialty', 'room')
    list_filter = ('status', 'specialty')


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
