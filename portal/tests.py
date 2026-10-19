import hashlib
import hmac
import json
import io
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .auth import MatricAuthenticationBackend
from .models import AcademicSession, AcademicTerm, FeeLineItem, FeeSchedule, IntegrationOutbox, Payment, PaymentWindow, User
from .services.payments import PaymentError, confirm_payment, valid_paystack_signature, verify_paystack_reference
from .services.registration_portal import RegistrationPortalAdapter


class PaymentServiceTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(username="PG/2026/001", matric_number="pg/2026/001", password="test-password", program="MSc", email="student@example.edu.ng")
        session = AcademicSession.objects.create(name="2026/2027")
        term = AcademicTerm.objects.create(session=session, semester="First", is_current=True)
        self.payment = Payment.objects.create(student=self.student, term=term, program="MSc", amount_kobo=7000000, reference="test-reference")

    @override_settings(PAYSTACK_SECRET_KEY="test-secret")
    def test_webhook_signature_uses_constant_time_hmac_digest(self):
        body = b'{"event":"charge.success"}'
        signature = hmac.new(b"test-secret", body, hashlib.sha512).hexdigest()
        self.assertTrue(valid_paystack_signature(body, signature))
        self.assertFalse(valid_paystack_signature(body, "bad-signature"))

    def test_confirmation_rejects_amount_reference_or_currency_mismatch(self):
        with self.assertRaises(PaymentError):
            confirm_payment(self.payment, {"reference": "test-reference", "amount": 1, "currency": "NGN"})
        with self.assertRaises(PaymentError):
            confirm_payment(self.payment, {"reference": "other", "amount": 7000000, "currency": "NGN"})

    def test_confirmation_is_idempotent_and_creates_one_outbox(self):
        payload = {"reference": "test-reference", "amount": 7000000, "currency": "NGN", "status": "success", "paid_at": timezone.now().isoformat()}
        first = confirm_payment(self.payment, payload)
        second = confirm_payment(self.payment, payload)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(status=Payment.Status.PAID).count(), 1)
        self.assertEqual(IntegrationOutbox.objects.filter(payment=self.payment).count(), 1)

    @override_settings(PAYSTACK_SECRET_KEY="test-secret")
    @patch("portal.services.payments.requests.get")
    def test_verification_checks_provider_amount_and_currency(self, get):
        response = Mock()
        response.json.return_value = {"status": True, "data": {"reference": "test-reference", "status": "success", "amount": 7000000, "currency": "NGN"}}
        get.return_value = response
        payment = verify_paystack_reference("test-reference")
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")


class MatricAuthenticationTests(TestCase):
    def test_matric_numbers_are_normalized_and_failed_attempts_lock(self):
        user = User.objects.create_user(username="abc/12/345", matric_number="abc/12/345", password="password")
        self.assertEqual(user.matric_number, "ABC/12/345")
        backend = MatricAuthenticationBackend()
        for _ in range(backend.max_attempts):
            self.assertIsNone(backend.authenticate(None, username="abc/12/345", password="wrong"))
        user.refresh_from_db()
        self.assertIsNotNone(user.locked_until)
        self.assertIsNone(backend.authenticate(None, username="ABC/12/345", password="password"))


@override_settings(PORTAL_API_URL="https://registration.example/api/payment", PORTAL_API_TOKEN="token", PORTAL_FIELD_MAPPING={"matric_number": "matric", "payment_reference": "reference"})
class RegistrationAdapterTests(TestCase):
    @patch("portal.services.registration_portal.requests.post")
    def test_payment_sync_uses_configured_field_mapping_and_idempotency_key(self, post):
        student = User.objects.create_user(username="PG/2026/010", matric_number="PG/2026/010", password="test-password", program="PGD")
        session = AcademicSession.objects.create(name="2026/2027")
        term = AcademicTerm.objects.create(session=session, semester="Second")
        payment = Payment.objects.create(student=student, term=term, program="PGD", amount_kobo=123450, reference="stable-ref", status=Payment.Status.PAID, paid_at=timezone.now())
        post.return_value = Mock(status_code=200)
        RegistrationPortalAdapter().sync_payment(payment)
        self.assertEqual(post.call_args.kwargs["json"]["matric"], "PG/2026/010")
        self.assertEqual(post.call_args.kwargs["json"]["reference"], "stable-ref")
        self.assertEqual(post.call_args.kwargs["headers"]["Idempotency-Key"], "stable-ref")


@override_settings(SECURE_SSL_REDIRECT=False)
class PortalViewTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(username="PG/2026/020", matric_number="PG/2026/020", password="test-password", program="MSc", email="student@example.edu.ng", first_name="Example")
        self.session = AcademicSession.objects.create(name="2026/2027")
        self.term = AcademicTerm.objects.create(session=self.session, semester="First", is_current=True)
        schedule = FeeSchedule.objects.create(term=self.term, program="MSc", is_published=True)
        FeeLineItem.objects.create(schedule=schedule, description="Course registration", amount_kobo=250000)
        PaymentWindow.objects.create(term=self.term, program="MSc", starts_at=timezone.now() - timedelta(minutes=1), ends_at=timezone.now() + timedelta(days=1), is_open=True)

    def test_student_dashboard_renders_server_side_fee(self):
        self.client.force_login(self.student)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PG/2026/020")
        self.assertContains(response, "2,500.00")
        self.assertContains(response, "term-logo")
        self.assertContains(response, "portal/acu-logo.png")

    def test_student_login_uses_university_crest_instead_of_initials(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "academic-stamp-logo")
        self.assertContains(response, "portal/acu-logo.png")
        self.assertNotContains(response, '<span>PG</span>', html=False)

    def test_staff_pages_render(self):
        staff = User.objects.create_superuser(username="report-admin", password="test-password")
        self.client.force_login(staff)
        admin_home = self.client.get("/django-admin/")
        self.assertRedirects(admin_home, "/staff/")
        console = self.client.get("/staff/")
        for section in ("Sessions, fees & payment windows", "Students & imports", "Payments & reports", "Clearance sync queue", "Audit log"):
            self.assertContains(console, section)
        record_admin = self.client.get("/django-admin/portal/academicterm/")
        self.assertEqual(record_admin.status_code, 200)
        self.assertContains(record_admin, "Ajayi Crowther University")
        self.assertContains(record_admin, "portal/acu-logo.png")
        for path in ("/staff/", "/staff/reports/", "/staff/students/", "/staff/students/upload/", "/staff/integrations/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_paid_receipt_is_a_pdf(self):
        payment = Payment.objects.create(student=self.student, term=self.term, program="MSc", amount_kobo=250000, reference="receipt-reference", status=Payment.Status.PAID, paid_at=timezone.now())
        self.client.force_login(self.student)
        response = self.client.get(f"/payments/{payment.pk}/receipt/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_staff_console_creates_and_activates_term(self):
        staff = User.objects.create_superuser(username="term-admin", password="test-password")
        self.client.force_login(staff)
        response = self.client.post("/staff/", {"action": "create_term", "session_name": "2027/2028", "semester": "Second", "make_current": "on"})
        self.assertRedirects(response, "/staff/#terms")
        new_term = AcademicTerm.objects.get(session__name="2027/2028", semester="Second")
        self.assertTrue(new_term.is_current)
        self.assertEqual(new_term.fee_schedules.count(), 3)
        self.assertEqual(new_term.windows.count(), 3)

    def test_staff_console_adds_second_semester_to_existing_session(self):
        staff = User.objects.create_superuser(username="semester-admin", password="test-password")
        previous_payment = Payment.objects.create(student=self.student, term=self.term, program="MSc", amount_kobo=250000, reference="previous-term-payment", status=Payment.Status.PAID, paid_at=timezone.now())
        self.client.force_login(staff)
        response = self.client.post("/staff/", {"action": "create_term", "session_name": "2026/2027", "semester": "Second", "make_current": "on"})
        self.assertRedirects(response, "/staff/#terms")
        second_term = AcademicTerm.objects.get(session=self.session, semester="Second")
        self.assertTrue(second_term.is_current)
        previous_payment.refresh_from_db()
        self.assertEqual(previous_payment.term, self.term)

    def test_staff_console_publishes_fee_lines(self):
        staff = User.objects.create_superuser(username="fees-admin", password="test-password")
        self.client.force_login(staff)
        response = self.client.post("/staff/", {"action": "save_fees", "term_id": self.term.pk, "program": "MSc", "fee_items": "Registration = 25000\nPortal = 1500.50", "is_published": "on"})
        self.assertRedirects(response, "/staff/#fees")
        schedule = FeeSchedule.objects.get(term=self.term, program="MSc")
        self.assertTrue(schedule.is_published)
        self.assertEqual(schedule.total_kobo, 2650050)

    def test_staff_console_creates_student_with_forced_password_change(self):
        staff = User.objects.create_superuser(username="student-admin", password="test-password")
        self.client.force_login(staff)
        response = self.client.post("/staff/", {"action": "save_student", "matric_number": "acu/pg/100", "program": "PGD", "surname": "Ade", "first_name": "Student", "other_names": "", "level_year": "Year 1", "department": "Business", "faculty": "Management", "email": "student@example.edu.ng", "phone": "+2348000000000", "is_active": "on"})
        self.assertEqual(response.status_code, 302)
        student = User.objects.get(matric_number="ACU/PG/100")
        self.assertTrue(student.check_password("ACU/PG/100"))
        self.assertTrue(student.must_change_password)

    @override_settings(DEBUG=True, DEMO_PAYMENTS=True, PAYSTACK_SECRET_KEY="")
    @patch("portal.services.payments.requests.post")
    def test_demo_payment_completes_locally_without_portal_sync(self, gateway_post):
        self.client.force_login(self.student)
        response = self.client.post("/payments/start/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/payments/demo/", response.url)
        checkout = self.client.get(response.url)
        self.assertContains(checkout, "does not contact Paystack or transfer funds")
        payment = Payment.objects.get(student=self.student, term=self.term)
        result = self.client.post(response.url, {"action": "complete"})
        self.assertRedirects(result, f"/payments/{payment.pk}/success/")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertEqual(payment.sync_status, Payment.SyncStatus.DEMO)
        self.assertFalse(IntegrationOutbox.objects.filter(payment=payment).exists())
        gateway_post.assert_not_called()
        success = self.client.get(result.url)
        self.assertContains(success, "No money was transferred.")


class SeedCommandTests(TestCase):
    @override_settings(DEBUG=True, SEED_SAMPLE_STUDENTS=False)
    def test_local_admin_is_created_with_forced_password_change(self):
        call_command("seed_portal", stdout=io.StringIO())
        admin = User.objects.get(username="admin")
        self.assertTrue(admin.check_password("admin"))
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.must_change_password)

    @override_settings(DEBUG=False, SEED_SAMPLE_STUDENTS=False)
    def test_default_admin_is_not_created_when_debug_is_off(self):
        call_command("seed_portal", stdout=io.StringIO())
        self.assertFalse(User.objects.filter(username="admin").exists())