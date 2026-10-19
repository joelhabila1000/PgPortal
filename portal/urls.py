from django.urls import path

from . import views

urlpatterns = [
    path("", views.student_login, name="login"),
    path("logout/", views.StudentLogoutView.as_view(), name="logout"),
    path("password/change/", views.password_change, name="password_change"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("payments/start/", views.start_payment, name="start_payment"),
    path("payments/demo/<uuid:payment_id>/", views.demo_checkout, name="demo_checkout"),
    path("payments/callback/", views.payment_callback, name="payment_callback"),
    path("payments/webhook/paystack/", views.paystack_webhook, name="webhook"),
    path("payments/<uuid:payment_id>/success/", views.payment_success, name="payment_success"),
    path("payments/<uuid:payment_id>/receipt/", views.receipt_pdf, name="receipt_pdf"),
    path("staff/", views.staff_home, name="staff_home"),
    path("staff/reports/", views.reports, name="reports"),
    path("staff/reports/export/", views.report_export, name="report_export"),
    path("staff/payments/<uuid:payment_id>/reconcile/", views.reconcile_payment, name="reconcile_payment"),
    path("staff/students/", views.students, name="students"),
    path("staff/students/new/", views.student_edit, name="student_new"),
    path("staff/students/<int:student_id>/edit/", views.student_edit, name="student_edit"),
    path("staff/students/export/", views.student_export, name="student_export"),
    path("staff/students/upload/", views.student_upload, name="student_upload"),
    path("staff/students/template/", views.student_template, name="student_template"),
    path("staff/integrations/", views.integration_queue, name="integration_queue"),
    path("staff/integrations/<int:outbox_id>/retry/", views.retry_integration, name="retry_integration"),
]