import csv
import io
from decimal import Decimal, InvalidOperation
from datetime import datetime
from urllib.parse import urlencode

import openpyxl
import qrcode
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LogoutView
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.core.validators import validate_email
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .forms import FirstPasswordChangeForm, MatricLoginForm, StudentForm, UploadForm
from .models import AcademicSession, AcademicTerm, AuditEvent, FeeLineItem, FeeSchedule, IntegrationOutbox, Payment, PaymentWindow, StudentImport, User
from .services.payments import PaymentError, parse_webhook_event, valid_paystack_signature, verify_paystack_reference
from .services.registration_portal import PortalIntegrationError, RegistrationPortalAdapter
from .tasks import sync_payment_to_portal


@never_cache
@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def student_login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = MatricLoginForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        user = authenticate(request, username=form.cleaned_data["matric_number"], password=form.cleaned_data["password"])
        if user:
            login(request, user)
            return redirect("password_change" if user.must_change_password else "dashboard")
        error = "The matric number or password is incorrect, or the account is temporarily locked."
    return render(request, "portal/login.html", {"form": form, "error": error})


@method_decorator(login_required, name="dispatch")
class StudentLogoutView(LogoutView):
    next_page = reverse_lazy("login")


@login_required
def password_change(request):
    form = FirstPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        AuditEvent.objects.create(actor=user, action="password_changed", target=user.matric_number or user.username)
        update_session_auth_hash(request, user)
        messages.success(request, "Your password has been changed.")
        return redirect("dashboard")
    return render(request, "portal/password_change.html", {"form": form})


@login_required
def dashboard(request):
    if request.user.is_staff:
        return redirect("staff_home")
    term = AcademicTerm.objects.filter(is_current=True).select_related("session").first()
    schedule = FeeSchedule.objects.filter(term=term, program=request.user.program, is_published=True).prefetch_related("items").first() if term else None
    window = term.windows.filter(program=request.user.program).first() if term else None
    payment = Payment.objects.filter(student=request.user, term=term, status=Payment.Status.PAID).first() if term else None
    history = request.user.payments.select_related("term__session")[:10]
    demo_payments_enabled = settings.DEBUG and settings.DEMO_PAYMENTS and not settings.PAYSTACK_SECRET_KEY
    return render(request, "portal/dashboard.html", {"term": term, "schedule": schedule, "window": window, "payment": payment, "history": history, "registration_url": settings.COURSE_REGISTRATION_URL, "demo_payments_enabled": demo_payments_enabled})


@login_required
@require_POST
def start_payment(request):
    if request.user.is_staff or not request.user.matric_number:
        raise Http404
    from .services.payments import initialize_demo_payment, initialize_payment

    try:
        if settings.DEBUG and settings.DEMO_PAYMENTS and not settings.PAYSTACK_SECRET_KEY:
            payment = initialize_demo_payment(request.user)
            return redirect("demo_checkout", payment_id=payment.pk)
        _, checkout_url = initialize_payment(request.user, request.build_absolute_uri(reverse("payment_callback")))
    except PaymentError as error:
        messages.error(request, str(error))
        return redirect("dashboard")
    return redirect(checkout_url)


@login_required
def demo_checkout(request, payment_id):
    if not settings.DEBUG or not settings.DEMO_PAYMENTS or settings.PAYSTACK_SECRET_KEY:
        raise Http404
    payment = get_object_or_404(Payment, pk=payment_id, student=request.user, gateway_response__test_mode=True)
    if payment.status == Payment.Status.PAID:
        return redirect("payment_success", payment_id=payment.pk)
    if payment.status != Payment.Status.INITIALIZED:
        messages.error(request, "This demo payment attempt is no longer active.")
        return redirect("dashboard")
    if request.POST.get("action") == "cancel":
        payment.status = Payment.Status.FAILED
        payment.gateway_response = {"test_mode": True, "status": "cancelled", "reference": payment.reference}
        payment.save(update_fields=["status", "gateway_response"])
        messages.info(request, "Demo payment cancelled. No funds were transferred.")
        return redirect("dashboard")
    if request.POST.get("action") != "complete":
        return render(request, "portal/demo_checkout.html", {"payment": payment})
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != Payment.Status.INITIALIZED or not payment.gateway_response.get("test_mode"):
            raise Http404
        payment.status = Payment.Status.PAID
        payment.paid_at = timezone.now()
        payment.sync_status = Payment.SyncStatus.DEMO
        payment.gateway_response = {"test_mode": True, "status": "success", "reference": payment.reference, "amount": payment.amount_kobo, "currency": "NGN", "channel": "demo"}
        payment.save(update_fields=["status", "paid_at", "sync_status", "gateway_response"])
        AuditEvent.objects.create(actor=request.user, action="demo_payment_simulated", target=payment.reference, details={"amount_kobo": payment.amount_kobo, "term": str(payment.term)})
    return redirect("payment_success", payment_id=payment.pk)


@login_required
def payment_callback(request):
    reference = request.GET.get("reference", "")
    payment = Payment.objects.filter(reference=reference).first()
    if not payment or (not request.user.is_staff and payment.student_id != request.user.pk):
        raise Http404
    try:
        verified = verify_paystack_reference(reference)
    except PaymentError as error:
        messages.error(request, str(error))
        return redirect("dashboard")
    return redirect("payment_success", payment_id=verified.pk)


@csrf_exempt
@require_POST
def paystack_webhook(request):
    if not valid_paystack_signature(request.body, request.headers.get("x-paystack-signature", "")):
        return HttpResponseBadRequest("Invalid signature")
    try:
        event = parse_webhook_event(request.body)
        event_name = event.get("event")
        event_data = event.get("data") or {}
        reference = event_data.get("reference")
        if event_name == "charge.success" and reference and Payment.objects.filter(reference=reference).exists():
            verify_paystack_reference(reference)
        elif event_name == "charge.failed" and reference:
            Payment.objects.filter(reference=reference, status=Payment.Status.INITIALIZED).update(status=Payment.Status.FAILED, gateway_response={"status": event_data.get("status", "failed"), "reference": reference})
    except PaymentError:
        return HttpResponse(status=503)
    return HttpResponse("ok")


@login_required
def payment_success(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("student", "term__session"), pk=payment_id)
    if not request.user.is_staff and payment.student_id != request.user.pk:
        raise Http404
    if payment.status != Payment.Status.PAID:
        return redirect("dashboard")
    return render(request, "portal/payment_success.html", {"payment": payment, "registration_url": settings.COURSE_REGISTRATION_URL, "is_demo": payment.gateway_response.get("test_mode", False)})


@login_required
def receipt_pdf(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("student", "term__session"), pk=payment_id, status=Payment.Status.PAID)
    if not request.user.is_staff and payment.student_id != request.user.pk:
        raise Http404
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    pdf.setTitle(f"Course registration fee receipt {payment.reference}")
    pdf.setFont("Helvetica-Bold", 19)
    pdf.drawString(52, 780, "POSTGRADUATE SCHOOL")
    pdf.setFont("Helvetica", 13)
    pdf.drawString(52, 752, "Course Registration Payment Receipt")
    rows = [
        ("Student", payment.student.full_name), ("Matric number", payment.student.matric_number),
        ("Programme", payment.get_program_display()), ("Session", payment.term.session.name),
        ("Semester", payment.term.get_semester_display()), ("Amount", f"NGN {payment.amount_naira:,.2f}"),
        ("Reference", payment.reference), ("Payment date", timezone.localtime(payment.paid_at).strftime("%d %b %Y, %I:%M %p")),
    ]
    y = 700
    for label, value in rows:
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(52, y, label)
        pdf.setFont("Helvetica", 10)
        pdf.drawString(170, y, str(value)[:90])
        y -= 31
    qr_image = qrcode.make(payment.reference)
    qr_buffer = io.BytesIO()
    qr_image.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)
    from reportlab.lib.utils import ImageReader

    pdf.drawImage(ImageReader(qr_buffer), 430, 515, width=100, height=100)
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(52, 450, "Verification code: " + payment.reference)
    pdf.drawString(52, 430, "Present this receipt at the course-registration portal if requested.")
    if payment.gateway_response.get("test_mode"):
        pdf.setFillColorRGB(0.65, 0.25, 0.05)
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(52, 398, "DEMO PAYMENT - NO FUNDS TRANSFERRED - NOT A CLEARANCE")
    pdf.showPage()
    pdf.save()
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="receipt-{payment.reference}.pdf"'
    return response


def _staff_check(user):
    return user.is_authenticated and user.is_staff


def admin_portal(request):
    if not request.user.is_authenticated:
        return redirect("admin:login")
    if not request.user.is_staff:
        raise PermissionDenied
    return redirect("staff_home")


def _student_admin_check(user):
    return _staff_check(user) and (user.is_superuser or user.has_perm("portal.change_user"))


def _integration_admin_check(user):
    return _staff_check(user) and (user.is_superuser or user.has_perm("portal.change_integrationoutbox"))


def _reconciliation_admin_check(user):
    return _staff_check(user) and (user.is_superuser or user.has_perm("portal.change_payment"))


def _require_staff_permission(request, permission):
    if not request.user.is_superuser and not request.user.has_perm(permission):
        raise PermissionDenied


def _staff_section_redirect(section, query=""):
    return redirect(reverse("staff_home") + (f"?{query}" if query else "") + f"#{section}")


def _student_import_batch(upload, actor):
    source_rows = _read_student_file(upload)
    required = {"matric_number", "surname", "first_name", "program", "department", "faculty", "level_year"}
    seen = set()
    rows, errors, warnings = [], [], []
    for number, raw in enumerate(source_rows, start=2):
        data = {str(key or "").strip().lower(): str(value or "").strip() for key, value in raw.items()}
        if "level/year" in data and "level_year" not in data:
            data["level_year"] = data.pop("level/year")
        data["matric_number"] = data.get("matric_number", "").upper()
        row_errors = []
        missing = sorted(field for field in required if not data.get(field))
        if missing:
            row_errors.append("Missing required: " + ", ".join(missing))
        if data.get("program") not in User.Program.values:
            row_errors.append("Program must be MSc, PGD, or PhD")
        if len(data["matric_number"]) > 40:
            row_errors.append("Matric number must be 40 characters or fewer")
        if data.get("email"):
            try:
                validate_email(data["email"])
            except ValidationError:
                row_errors.append("Email address is invalid")
        if data["matric_number"] in seen:
            row_errors.append("Duplicate matric number in upload")
        seen.add(data["matric_number"])
        if row_errors:
            errors.append({"row": number, "reasons": row_errors})
            continue
        values = {key: data.get(key, "") for key in ("surname", "first_name", "other_names", "program", "department", "faculty", "level_year", "email", "phone")}
        rows.append({"row": number, "data": {"matric_number": data["matric_number"], **values}})
    if rows:
        try:
            existing = RegistrationPortalAdapter().verify_students([row["data"]["matric_number"] for row in rows])
            for row in rows:
                if existing is None:
                    warnings.append({"row": row["row"], "reason": "Registration portal matric check is not configured"})
                elif row["data"]["matric_number"] not in existing:
                    warnings.append({"row": row["row"], "reason": "Matric number was not found in the registration portal"})
        except PortalIntegrationError as error:
            warnings.extend({"row": row["row"], "reason": str(error)} for row in rows)
    batch = StudentImport.objects.create(created_by=actor, filename=upload.name[:255], rows=rows, errors=errors, warnings=warnings)
    AuditEvent.objects.create(actor=actor, action="student_import_uploaded", target=batch.filename, details={"valid_rows": len(rows), "error_rows": len(errors), "warning_count": len(warnings)})
    return batch


def _commit_student_import(batch, actor, reset_passwords=False):
    with transaction.atomic():
        for row in batch.rows:
            values = row["data"]
            student, created = User.objects.get_or_create(matric_number=values["matric_number"], defaults={key: value for key, value in values.items() if key != "matric_number"})
            if not created:
                for key, value in values.items():
                    if key != "matric_number":
                        setattr(student, key, value)
            if created or reset_passwords:
                student.set_password(student.matric_number)
                student.must_change_password = True
            student.save()
        batch.committed_at = timezone.now()
        batch.save(update_fields=["committed_at"])
        AuditEvent.objects.create(actor=actor, action="student_import_committed", target=batch.filename, details={"count": len(batch.rows), "passwords_reset": reset_passwords})


@user_passes_test(_staff_check)
def staff_home(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        permissions = {
            "create_term": "portal.change_academicterm", "activate_term": "portal.change_academicterm",
            "save_window": "portal.change_paymentwindow", "save_fees": "portal.change_feeschedule",
            "save_student": "portal.change_user", "deactivate_student": "portal.change_user", "reset_student": "portal.change_user",
            "preview_import": "portal.change_user", "commit_import": "portal.change_user",
            "retry_sync": "portal.change_integrationoutbox", "reconcile_payment": "portal.change_payment",
        }
        if action in permissions:
            _require_staff_permission(request, permissions[action])
        try:
            if action == "create_term":
                name = request.POST.get("session_name", "").strip()
                semester = request.POST.get("semester", "")
                if len(name) != 9 or name[4] != "/" or not name[:4].isdigit() or not name[5:].isdigit():
                    raise ValueError("Session must use the YYYY/YYYY format.")
                if semester not in AcademicTerm.Semester.values:
                    raise ValueError("Choose First or Second semester.")
                session, _ = AcademicSession.objects.get_or_create(name=name)
                term, created = AcademicTerm.objects.get_or_create(session=session, semester=semester)
                if request.POST.get("make_current") == "on":
                    with transaction.atomic():
                        AcademicTerm.objects.exclude(pk=term.pk).update(is_current=False)
                        AcademicTerm.objects.filter(pk=term.pk).update(is_current=True)
                    AuditEvent.objects.create(actor=request.user, action="active_term_changed", target=str(term))
                if created:
                    for program in User.Program.values:
                        FeeSchedule.objects.get_or_create(term=term, program=program)
                        PaymentWindow.objects.get_or_create(term=term, program=program, defaults={"starts_at": timezone.now(), "ends_at": timezone.now(), "is_open": False})
                AuditEvent.objects.create(actor=request.user, action="academic_term_created", target=str(term))
                messages.success(request, f"Academic term {term} is ready.")
                return _staff_section_redirect("terms")

            if action == "activate_term":
                term = get_object_or_404(AcademicTerm, pk=request.POST.get("term_id"))
                with transaction.atomic():
                    AcademicTerm.objects.exclude(pk=term.pk).update(is_current=False)
                    AcademicTerm.objects.filter(pk=term.pk).update(is_current=True)
                AuditEvent.objects.create(actor=request.user, action="active_term_changed", target=str(term))
                messages.success(request, f"Active term changed to {term}.")
                return _staff_section_redirect("terms")

            if action == "save_window":
                term = get_object_or_404(AcademicTerm, pk=request.POST.get("term_id"))
                program = request.POST.get("program", "")
                if program not in User.Program.values:
                    raise ValueError("Choose MSc, PGD, or PhD.")
                starts = datetime.fromisoformat(request.POST["starts_at"])
                ends = datetime.fromisoformat(request.POST["ends_at"])
                starts = timezone.make_aware(starts) if timezone.is_naive(starts) else starts
                ends = timezone.make_aware(ends) if timezone.is_naive(ends) else ends
                if starts >= ends:
                    raise ValueError("The payment window end must be after its start.")
                window, _ = PaymentWindow.objects.update_or_create(term=term, program=program, defaults={"starts_at": starts, "ends_at": ends, "is_open": request.POST.get("is_open") == "on"})
                AuditEvent.objects.create(actor=request.user, action="payment_window_updated", target=f"{term} · {program}", details={"is_open": window.is_open})
                messages.success(request, f"{program} payment window saved.")
                return _staff_section_redirect("terms")

            if action == "save_fees":
                term = get_object_or_404(AcademicTerm, pk=request.POST.get("term_id"))
                program = request.POST.get("program", "")
                if program not in User.Program.values:
                    raise ValueError("Choose MSc, PGD, or PhD.")
                parsed_items = []
                for line in request.POST.get("fee_items", "").splitlines():
                    if not line.strip():
                        continue
                    description, separator, raw_amount = line.partition("=")
                    if not separator or not description.strip():
                        raise ValueError("Enter one fee per line as Description = amount in NGN.")
                    amount = Decimal(raw_amount.strip())
                    kobo = amount * 100
                    if kobo <= 0 or kobo != kobo.to_integral_value():
                        raise ValueError("Fee amounts must be positive and use no more than two decimal places.")
                    parsed_items.append((description.strip()[:160], int(kobo)))
                if not parsed_items:
                    raise ValueError("Add at least one fee line item.")
                with transaction.atomic():
                    schedule, _ = FeeSchedule.objects.get_or_create(term=term, program=program)
                    schedule.items.all().delete()
                    FeeLineItem.objects.bulk_create([FeeLineItem(schedule=schedule, description=description, amount_kobo=amount) for description, amount in parsed_items])
                    schedule.is_published = request.POST.get("is_published") == "on"
                    schedule.save(update_fields=["is_published"])
                    AuditEvent.objects.create(actor=request.user, action="fee_schedule_updated", target=f"{term} · {program}", details={"line_count": len(parsed_items), "published": schedule.is_published})
                messages.success(request, f"{program} fee schedule saved.")
                return _staff_section_redirect("fees")

            if action == "save_student":
                student_id = request.POST.get("student_id")
                student = get_object_or_404(User, pk=student_id, is_staff=False) if student_id else None
                form = StudentForm(request.POST, instance=student)
                if not form.is_valid():
                    raise ValueError("Check the student fields and try again.")
                with transaction.atomic():
                    student = form.save(commit=False)
                    created = student.pk is None
                    student.save()
                    reset = created or request.POST.get("reset_password") == "on"
                    if reset:
                        student.set_password(student.matric_number)
                        student.must_change_password = True
                        student.save(update_fields=["password", "must_change_password"])
                    AuditEvent.objects.create(actor=request.user, action="student_created" if created else "student_updated", target=student.matric_number, details={"password_reset": reset})
                messages.success(request, f"Student {student.matric_number} saved.")
                return _staff_section_redirect("students", urlencode({"edit_student": student.pk}))

            if action in {"deactivate_student", "reset_student"}:
                student = get_object_or_404(User, pk=request.POST.get("student_id"), is_staff=False)
                if action == "deactivate_student":
                    student.is_active = False
                    student.save(update_fields=["is_active"])
                    AuditEvent.objects.create(actor=request.user, action="student_deactivated", target=student.matric_number)
                    messages.success(request, f"{student.matric_number} deactivated.")
                else:
                    student.set_password(student.matric_number)
                    student.must_change_password = True
                    student.failed_login_attempts = 0
                    student.locked_until = None
                    student.save(update_fields=["password", "must_change_password", "failed_login_attempts", "locked_until"])
                    AuditEvent.objects.create(actor=request.user, action="password_reset_to_matric", target=student.matric_number)
                    messages.success(request, f"Password reset for {student.matric_number}.")
                return _staff_section_redirect("students")

            if action == "preview_import":
                form = UploadForm(request.POST, request.FILES)
                if not form.is_valid():
                    raise ValueError("Choose a CSV or XLSX file to preview.")
                batch = _student_import_batch(form.cleaned_data["file"], request.user)
                return _staff_section_redirect("students", urlencode({"batch": batch.pk}))

            if action == "commit_import":
                batch = get_object_or_404(StudentImport, pk=request.POST.get("batch_id"), created_by=request.user, committed_at__isnull=True)
                if batch.errors:
                    raise ValueError("Resolve validation errors before committing this upload.")
                _commit_student_import(batch, request.user, request.POST.get("reset_passwords") == "on")
                messages.success(request, f"Imported {len(batch.rows)} student records.")
                return _staff_section_redirect("students")

            if action == "retry_sync":
                outbox = get_object_or_404(IntegrationOutbox.objects.select_related("payment"), pk=request.POST.get("outbox_id"))
                outbox.status = IntegrationOutbox.Status.PENDING
                outbox.attempts = 0
                outbox.next_attempt_at = timezone.now()
                outbox.save(update_fields=["status", "attempts", "next_attempt_at", "updated_at"])
                Payment.objects.filter(pk=outbox.payment_id).update(sync_status=Payment.SyncStatus.PENDING)
                try:
                    sync_payment_to_portal.delay(outbox.pk)
                except Exception:
                    pass
                AuditEvent.objects.create(actor=request.user, action="integration_retry", target=outbox.payment.reference)
                messages.success(request, "Payment sync has been queued for retry.")
                return _staff_section_redirect("sync")

            if action == "reconcile_payment":
                payment = get_object_or_404(Payment, pk=request.POST.get("payment_id"))
                verified = verify_paystack_reference(payment.reference)
                AuditEvent.objects.create(actor=request.user, action="payment_reconciled", target=payment.reference, details={"payment_status": verified.status})
                messages.success(request, f"Gateway reports {verified.get_status_display()} for {verified.reference}.")
                return _staff_section_redirect("payments")
        except (ValueError, InvalidOperation, KeyError, PaymentError, UnicodeDecodeError, openpyxl.utils.exceptions.InvalidFileException) as error:
            messages.error(request, str(error))
            section = "students" if action in {"save_student", "deactivate_student", "reset_student", "preview_import", "commit_import"} else "terms" if action in {"create_term", "activate_term", "save_window"} else "fees" if action == "save_fees" else "sync" if action == "retry_sync" else "payments"
            return _staff_section_redirect(section)

    term = AcademicTerm.objects.filter(is_current=True).select_related("session").first()
    students = User.objects.filter(is_active=True, is_staff=False, program__in=User.Program.values)
    payments = Payment.objects.filter(term=term).select_related("student", "term__session") if term else Payment.objects.none()
    paid = payments.filter(status=Payment.Status.PAID)
    paid_student_ids = paid.values_list("student_id", flat=True)
    totals = paid.aggregate(amount=Sum("amount_kobo"))
    program_summary = []
    for program, label in User.Program.choices:
        program_students = students.filter(program=program)
        program_paid = paid.filter(program=program)
        count = program_paid.count()
        student_count = program_students.count()
        program_summary.append({"program": program, "label": label, "paid": count, "students": student_count, "percent": round(count * 100 / student_count) if student_count else 0, "amount": (program_paid.aggregate(amount=Sum("amount_kobo"))["amount"] or 0) / 100})
    student_records = User.objects.filter(is_staff=False, matric_number__isnull=False).order_by("matric_number")
    student_query = request.GET.get("q", "").strip()
    if student_query:
        student_records = student_records.filter(Q(matric_number__icontains=student_query) | Q(surname__icontains=student_query) | Q(first_name__icontains=student_query) | Q(email__icontains=student_query))
    student_program = request.GET.get("program", "")
    if student_program in User.Program.values:
        student_records = student_records.filter(program=student_program)
    student_status = request.GET.get("status", "")
    if student_status == "active":
        student_records = student_records.filter(is_active=True)
    elif student_status == "inactive":
        student_records = student_records.filter(is_active=False)
    edit_student = User.objects.filter(pk=request.GET.get("edit_student"), is_staff=False).first() if request.GET.get("edit_student") else None
    import_batch = StudentImport.objects.filter(pk=request.GET.get("batch"), created_by=request.user, committed_at__isnull=True).first() if request.GET.get("batch") else None
    staff_terms = AcademicTerm.objects.select_related("session").order_by("-session__name", "semester")
    payment_term = staff_terms.filter(pk=request.GET.get("payment_term")).first() if request.GET.get("payment_term") else term
    transaction_records = Payment.objects.select_related("student", "term__session").filter(term=payment_term).order_by("-created_at")[:30] if payment_term else Payment.objects.none()
    context = {
        "term": term,
        "student_count": students.count(),
        "paid_count": paid.count(),
        "unpaid_count": students.exclude(pk__in=paid_student_ids).count() if term else 0,
        "collected": (totals["amount"] or 0) / 100,
        "sync_attention": IntegrationOutbox.objects.exclude(status=IntegrationOutbox.Status.SYNCED).count(),
        "open_windows": term.windows.filter(is_open=True, starts_at__lte=timezone.now(), ends_at__gte=timezone.now()).count() if term else 0,
        "program_summary": program_summary,
        "recent_payments": payments.order_by("-created_at")[:7],
        "recent_audit": AuditEvent.objects.select_related("actor")[:5],
        "staff_terms": staff_terms,
        "windows": PaymentWindow.objects.select_related("term__session").order_by("-term__session__name", "program"),
        "fee_schedules": FeeSchedule.objects.select_related("term__session").prefetch_related("items").order_by("-term__session__name", "program"),
        "student_page": Paginator(student_records, 25).get_page(request.GET.get("page")),
        "student_form": StudentForm(instance=edit_student),
        "edit_student": edit_student,
        "import_form": UploadForm(),
        "import_batch": import_batch,
        "transaction_records": transaction_records,
        "payment_term": payment_term,
        "all_outbox_records": IntegrationOutbox.objects.select_related("payment__student", "payment__term__session").order_by("status", "-updated_at")[:30],
        "all_audit_records": AuditEvent.objects.select_related("actor")[:25],
        "program_choices": User.Program.choices,
    }
    return render(request, "portal/staff_home.html", context)


@user_passes_test(_staff_check)
def reports(request):
    terms = AcademicTerm.objects.select_related("session").all()
    selected_term = terms.filter(pk=request.GET.get("term")).first() if request.GET.get("term") else terms.filter(is_current=True).first()
    payments = Payment.objects.select_related("student", "term__session")
    if selected_term:
        payments = payments.filter(term=selected_term)
    if request.GET.get("program") in User.Program.values:
        payments = payments.filter(program=request.GET["program"])
    paid = payments.filter(status=Payment.Status.PAID)
    summaries = []
    for program, label in User.Program.choices:
        scoped = paid.filter(program=program)
        summaries.append({"program": label, "count": scoped.count(), "amount": sum(item.amount_kobo for item in scoped) / 100})
    students = User.objects.filter(is_active=True, is_staff=False, program__in=User.Program.values)
    if request.GET.get("program") in User.Program.values:
        students = students.filter(program=request.GET["program"])
    if selected_term:
        paid_students = paid.values_list("student_id", flat=True)
        unpaid_count = students.exclude(pk__in=paid_students).count()
        student_count = students.count()
    else:
        unpaid_count = 0
        student_count = students.count()
    context = {"terms": terms, "selected_term": selected_term, "summaries": summaries, "paid_count": paid.count(), "payment_count": payments.count(), "student_count": student_count, "unpaid_count": unpaid_count, "payments": payments[:50], "outbox_failures": IntegrationOutbox.objects.filter(status=IntegrationOutbox.Status.FAILED).count()}
    return render(request, "portal/reports.html", context)


@user_passes_test(_staff_check)
def report_export(request):
    term = AcademicTerm.objects.filter(pk=request.GET.get("term")).first() if request.GET.get("term") else AcademicTerm.objects.filter(is_current=True).first()
    students = User.objects.filter(is_active=True, is_staff=False, matric_number__isnull=False)
    if request.GET.get("program") in User.Program.values:
        students = students.filter(program=request.GET["program"])
    paid_by_student = {}
    if term:
        paid_by_student = {payment.student_id: payment for payment in Payment.objects.filter(term=term, status=Payment.Status.PAID).select_related("student")}
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="course-fee-reconciliation.csv"'
    writer = csv.writer(response)
    writer.writerow(["matric_number", "student", "program", "session", "semester", "amount_ngn", "reference", "payment_status", "sync_status", "payment_date"])
    for student in students.iterator():
        payment = paid_by_student.get(student.pk)
        row = [student.matric_number, student.full_name, student.program, term.session.name if term else "", term.semester if term else "", f"{payment.amount_naira:.2f}" if payment else "", payment.reference if payment else "", "paid" if payment else ("unpaid" if term else "no active term"), payment.sync_status if payment else "", payment.paid_at.isoformat() if payment and payment.paid_at else ""]
        writer.writerow([_safe_csv_cell(value) for value in row])
    return response


def _safe_csv_cell(value):
    text = str(value or "")
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _read_student_file(upload):
    if upload.size > 10 * 1024 * 1024:
        raise ValueError("File exceeds the 10 MB limit.")
    suffix = upload.name.rsplit(".", 1)[-1].lower()
    if suffix == "csv":
        text = upload.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = {str(header or "").strip().lower() for header in (reader.fieldnames or [])}
        required = {"matric_number", "surname", "first_name", "program", "department", "faculty"}
        if not required.issubset(headers):
            raise ValueError("Missing required headers: " + ", ".join(sorted(required - headers)))
        rows = list(reader)
        if len(rows) > 20000:
            raise ValueError("File exceeds the 20,000 row limit.")
        if not rows:
            raise ValueError("The upload contains no student rows.")
        return rows
    if suffix == "xlsx":
        workbook = openpyxl.load_workbook(upload, read_only=True, data_only=True)
        sheet = workbook.active
        values = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in next(values, [])]
        required = {"matric_number", "surname", "first_name", "program", "department", "faculty"}
        normalized_headers = {header.lower() for header in headers}
        if not required.issubset(normalized_headers):
            raise ValueError("Missing required headers: " + ", ".join(sorted(required - normalized_headers)))
        rows = [dict(zip(headers, row)) for row in values]
        if len(rows) > 20000:
            raise ValueError("File exceeds the 20,000 row limit.")
        if not rows:
            raise ValueError("The upload contains no student rows.")
        return rows
    raise ValueError("Upload a .csv or .xlsx file.")


@user_passes_test(_student_admin_check)
def student_upload(request):
    if request.method == "POST" and "commit" in request.POST:
        batch = get_object_or_404(StudentImport, pk=request.POST.get("batch"), created_by=request.user, committed_at__isnull=True)
        if batch.errors:
            messages.error(request, "This import has validation errors and cannot be committed.")
            return redirect("student_upload")
        reset = request.POST.get("reset_passwords") == "on"
        with transaction.atomic():
            for row in batch.rows:
                values = row["data"]
                student, created = User.objects.get_or_create(matric_number=values["matric_number"], defaults={key: value for key, value in values.items() if key != "matric_number"})
                if not created:
                    for key, value in values.items():
                        if key != "matric_number":
                            setattr(student, key, value)
                if created or reset:
                    student.set_password(student.matric_number)
                    student.must_change_password = True
                student.save()
            batch.committed_at = timezone.now()
            batch.save(update_fields=["committed_at"])
            AuditEvent.objects.create(actor=request.user, action="student_import_committed", target=batch.filename, details={"count": len(batch.rows), "passwords_reset": reset})
        messages.success(request, f"Imported {len(batch.rows)} student records.")
        return redirect("student_upload")

    form = UploadForm(request.POST or None, request.FILES or None)
    batch = None
    if request.method == "POST" and form.is_valid():
        try:
            source_rows = _read_student_file(form.cleaned_data["file"])
        except (ValueError, UnicodeDecodeError, openpyxl.utils.exceptions.InvalidFileException) as error:
            form.add_error("file", str(error))
            source_rows = []
        required = {"matric_number", "surname", "first_name", "program", "department", "faculty", "level_year"}
        seen = set()
        rows, errors, warnings = [], [], []
        for number, raw in enumerate(source_rows, start=2):
            data = {str(key or "").strip().lower(): str(value or "").strip() for key, value in raw.items()}
            if "level/year" in data and "level_year" not in data:
                data["level_year"] = data.pop("level/year")
            data["matric_number"] = data.get("matric_number", "").upper()
            row_errors = []
            missing = sorted(field for field in required if not data.get(field))
            if missing:
                row_errors.append("Missing required: " + ", ".join(missing))
            if data.get("program") not in User.Program.values:
                row_errors.append("Program must be MSc, PGD, or PhD")
            if len(data["matric_number"]) > 40:
                row_errors.append("Matric number must be 40 characters or fewer")
            if data.get("email"):
                try:
                    validate_email(data["email"])
                except ValidationError:
                    row_errors.append("Email address is invalid")
            if data["matric_number"] in seen:
                row_errors.append("Duplicate matric number in upload")
            seen.add(data["matric_number"])
            if row_errors:
                errors.append({"row": number, "reasons": row_errors})
                continue
            values = {key: data.get(key, "") for key in ("surname", "first_name", "other_names", "program", "department", "faculty", "level_year", "email", "phone")}
            rows.append({"row": number, "data": {"matric_number": data["matric_number"], **values}})
        if rows:
            try:
                existing_matric = RegistrationPortalAdapter().verify_students([row["data"]["matric_number"] for row in rows])
                for row in rows:
                    if existing_matric is None:
                        warnings.append({"row": row["row"], "reason": "Registration portal matric check is not configured"})
                    elif row["data"]["matric_number"] not in existing_matric:
                        warnings.append({"row": row["row"], "reason": "Matric number was not found in the registration portal"})
            except PortalIntegrationError as error:
                warnings.extend({"row": row["row"], "reason": str(error)} for row in rows)
        if source_rows:
            batch = StudentImport.objects.create(created_by=request.user, filename=form.cleaned_data["file"].name[:255], rows=rows, errors=errors, warnings=warnings)
            AuditEvent.objects.create(actor=request.user, action="student_import_uploaded", target=batch.filename, details={"valid_rows": len(rows), "error_rows": len(errors), "warning_count": len(warnings)})
    recent = StudentImport.objects.filter(created_by=request.user)[:10]
    return render(request, "portal/student_upload.html", {"form": form, "batch": batch, "recent": recent})


@user_passes_test(_student_admin_check)
def student_template(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="student-upload-template.csv"'
    writer = csv.writer(response)
    writer.writerow(["matric_number", "surname", "first_name", "other_names", "program", "department", "faculty", "level_year", "email", "phone"])
    writer.writerow(["PG/2026/001", "Example", "Student", "", "MSc", "Computer Science", "Science", "Year 1", "student@example.edu.ng", "+2348000000000"])
    return response


@user_passes_test(_student_admin_check)
def students(request):
    records = User.objects.filter(is_staff=False, matric_number__isnull=False).order_by("matric_number")
    query = request.GET.get("q", "").strip()
    if query:
        records = records.filter(Q(matric_number__icontains=query) | Q(surname__icontains=query) | Q(first_name__icontains=query) | Q(email__icontains=query))
    if request.GET.get("program") in User.Program.values:
        records = records.filter(program=request.GET["program"])
    if request.GET.get("status") == "inactive":
        records = records.filter(is_active=False)
    elif request.GET.get("status") == "active":
        records = records.filter(is_active=True)
    page = Paginator(records, 50).get_page(request.GET.get("page"))
    return render(request, "portal/students.html", {"page": page, "program_choices": User.Program.choices})


@user_passes_test(_student_admin_check)
def student_edit(request, student_id=None):
    student = get_object_or_404(User, pk=student_id, is_staff=False) if student_id else None
    form = StudentForm(request.POST or None, instance=student)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            is_new = saved.pk is None
            saved.save()
            reset = is_new or request.POST.get("reset_password") == "on"
            if reset:
                saved.set_password(saved.matric_number)
                saved.must_change_password = True
                saved.save(update_fields=["password", "must_change_password"])
            AuditEvent.objects.create(actor=request.user, action="student_created" if is_new else "student_updated", target=saved.matric_number, details={"password_reset": reset, "is_active": saved.is_active})
        messages.success(request, "Student record saved.")
        return redirect("students")
    return render(request, "portal/student_form.html", {"form": form, "student": student})


@user_passes_test(_student_admin_check)
def student_export(request):
    records = User.objects.filter(is_staff=False, matric_number__isnull=False).order_by("matric_number")
    query = request.GET.get("q", "").strip()
    if query:
        records = records.filter(Q(matric_number__icontains=query) | Q(surname__icontains=query) | Q(first_name__icontains=query) | Q(email__icontains=query))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="students.csv"'
    writer = csv.writer(response)
    writer.writerow(["matric_number", "surname", "first_name", "other_names", "program", "department", "faculty", "level_year", "email", "phone", "is_active"])
    for student in records.iterator():
        writer.writerow([_safe_csv_cell(value) for value in [student.matric_number, student.surname, student.first_name, student.other_names, student.program, student.department, student.faculty, student.level_year, student.email, student.phone, student.is_active]])
    return response


@user_passes_test(_reconciliation_admin_check)
def reconcile_payment(request, payment_id):
    payment = get_object_or_404(Payment, pk=payment_id)
    try:
        verified = verify_paystack_reference(payment.reference)
    except PaymentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Gateway reports {verified.get_status_display()} for {verified.reference}.")
        AuditEvent.objects.create(actor=request.user, action="payment_reconciled", target=payment.reference, details={"payment_status": verified.status})
    return redirect("reports")


@user_passes_test(_staff_check)
def integration_queue(request):
    entries = IntegrationOutbox.objects.select_related("payment__student", "payment__term__session").order_by("status", "next_attempt_at")
    paginator = Paginator(entries, 50)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "portal/integration_queue.html", {"page": page})


@user_passes_test(_integration_admin_check)
@require_POST
def retry_integration(request, outbox_id):
    outbox = get_object_or_404(IntegrationOutbox.objects.select_related("payment"), pk=outbox_id)
    outbox.status = IntegrationOutbox.Status.PENDING
    outbox.attempts = 0
    outbox.next_attempt_at = timezone.now()
    outbox.save(update_fields=["status", "attempts", "next_attempt_at", "updated_at"])
    Payment.objects.filter(pk=outbox.payment_id).update(sync_status=Payment.SyncStatus.PENDING)
    sync_payment_to_portal.delay(outbox.pk)
    AuditEvent.objects.create(actor=request.user, action="integration_retry", target=outbox.payment.reference)
    messages.success(request, "Payment sync queued for retry.")
    return redirect("integration_queue")