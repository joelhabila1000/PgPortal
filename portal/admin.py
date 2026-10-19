from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone

from .models import AcademicSession, AcademicTerm, AuditEvent, FeeLineItem, FeeSchedule, IntegrationOutbox, Payment, PaymentWindow, User


class AuditAdminMixin:
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        AuditEvent.objects.create(actor=request.user, action=f"{obj._meta.model_name}_{'updated' if change else 'created'}", target=str(obj))

    def delete_model(self, request, obj):
        target = str(obj)
        super().delete_model(request, obj)
        AuditEvent.objects.create(actor=request.user, action=f"{obj._meta.model_name}_deleted", target=target)

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        if formset.model is FeeLineItem:
            AuditEvent.objects.create(actor=request.user, action="fee_line_items_updated", target=str(form.instance))


class FeeLineItemInline(admin.TabularInline):
    model = FeeLineItem
    extra = 1


@admin.register(User)
class StudentAdmin(UserAdmin):
    list_display = ("matric_number", "surname", "first_name", "program", "department", "is_active", "must_change_password")
    list_filter = ("program", "is_active", "must_change_password", "is_staff")
    search_fields = ("matric_number", "surname", "first_name", "email", "department", "faculty")
    ordering = ("matric_number",)
    fieldsets = ((None, {"fields": ("username", "password")}), ("Student profile", {"fields": ("matric_number", "surname", "first_name", "other_names", "program", "department", "faculty", "level_year", "email", "phone")}), ("Access", {"fields": ("is_active", "is_staff", "is_superuser", "must_change_password", "failed_login_attempts", "locked_until", "groups", "user_permissions")}))
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("username", "matric_number", "password1", "password2", "program", "is_staff")})),
    actions = ["reset_password_to_matric"]

    @admin.action(description="Reset selected student passwords to matric number")
    def reset_password_to_matric(self, request, queryset):
        count = 0
        for student in queryset.exclude(matric_number__isnull=True).exclude(matric_number=""):
            student.set_password(student.matric_number)
            student.must_change_password = True
            student.failed_login_attempts = 0
            student.locked_until = None
            student.save(update_fields=["password", "must_change_password", "failed_login_attempts", "locked_until"])
            AuditEvent.objects.create(actor=request.user, action="password_reset_to_matric", target=student.matric_number)
            count += 1
        self.message_user(request, f"Reset {count} student password(s).", messages.SUCCESS)


@admin.register(AcademicSession)
class AcademicSessionAdmin(AuditAdminMixin, admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at")
    search_fields = ("name",)


@admin.register(AcademicTerm)
class AcademicTermAdmin(AuditAdminMixin, admin.ModelAdmin):
    list_display = ("session", "semester", "is_current")
    list_filter = ("is_current", "semester")
    actions = ["make_current"]

    @admin.action(description="Set selected session and semester as active")
    def make_current(self, request, queryset):
        term = queryset.first()
        if not term:
            return
        AcademicTerm.objects.exclude(pk=term.pk).update(is_current=False)
        term.is_current = True
        term.save(update_fields=["is_current"])
        AuditEvent.objects.create(actor=request.user, action="active_term_changed", target=str(term))
        self.message_user(request, f"Active term set to {term}.", messages.SUCCESS)


@admin.register(PaymentWindow)
class PaymentWindowAdmin(AuditAdminMixin, admin.ModelAdmin):
    list_display = ("term", "program", "starts_at", "ends_at", "is_open")
    list_filter = ("program", "is_open", "term__session")


@admin.register(FeeSchedule)
class FeeScheduleAdmin(AuditAdminMixin, admin.ModelAdmin):
    list_display = ("term", "program", "is_published", "total_naira")
    list_filter = ("program", "is_published", "term__session")
    inlines = [FeeLineItemInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "student", "program", "term", "amount_naira", "status", "sync_status", "paid_at")
    list_filter = ("status", "sync_status", "program", "term__session", "term__semester")
    search_fields = ("reference", "student__matric_number", "student__surname")
    readonly_fields = ("id", "student", "term", "program", "amount_kobo", "reference", "status", "sync_status", "gateway_response", "paid_at", "created_at")


@admin.register(IntegrationOutbox)
class IntegrationOutboxAdmin(admin.ModelAdmin):
    list_display = ("payment", "status", "attempts", "next_attempt_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("payment__reference", "payment__student__matric_number", "last_error")
    readonly_fields = ("payment", "attempts", "last_error", "updated_at")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target")
    list_filter = ("action", "created_at")
    search_fields = ("target", "actor__username")
    readonly_fields = ("actor", "action", "target", "details", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False