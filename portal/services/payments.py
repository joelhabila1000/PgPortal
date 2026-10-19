import hashlib
import hmac
import json
from datetime import timezone as datetime_timezone

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from portal.models import AcademicTerm, FeeSchedule, IntegrationOutbox, Payment, User

PAYSTACK_API = "https://api.paystack.co"


class PaymentError(Exception):
    pass


def initialize_demo_payment(student):
    term = AcademicTerm.objects.filter(is_current=True).first()
    if not term:
        raise PaymentError("There is no active session and semester.")
    window = term.windows.filter(program=student.program).first()
    if not window or not window.currently_open:
        raise PaymentError("Payment is not open for your programme.")
    schedule = FeeSchedule.objects.filter(term=term, program=student.program, is_published=True).prefetch_related("items").first()
    if not schedule or not schedule.items.exists() or schedule.total_kobo <= 0:
        raise PaymentError("A published fee schedule is not available for your programme.")
    if Payment.objects.filter(student=student, term=term, status=Payment.Status.PAID).exists():
        raise PaymentError("A payment is already confirmed for this session and semester.")
    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=student.pk)
            if Payment.objects.filter(student=student, term=term, status=Payment.Status.INITIALIZED).exists():
                raise PaymentError("A payment attempt is already in progress. Please wait for it to finish.")
            return Payment.objects.create(
                student=student,
                term=term,
                program=student.program,
                amount_kobo=schedule.total_kobo,
                gateway_response={"test_mode": True, "status": "initialized"},
            )
    except IntegrityError as error:
        raise PaymentError("A payment attempt is already in progress. Please wait for it to finish.") from error


def initialize_payment(student, callback_url):
    term = AcademicTerm.objects.filter(is_current=True).first()
    if not term:
        raise PaymentError("There is no active session and semester.")
    window = term.windows.filter(program=student.program).first()
    if not window or not window.currently_open:
        raise PaymentError("Payment is not open for your programme.")
    schedule = FeeSchedule.objects.filter(term=term, program=student.program, is_published=True).prefetch_related("items").first()
    if not schedule or not schedule.items.exists() or schedule.total_kobo <= 0:
        raise PaymentError("A published fee schedule is not available for your programme.")
    if Payment.objects.filter(student=student, term=term, status=Payment.Status.PAID).exists():
        raise PaymentError("A payment is already confirmed for this session and semester.")
    if not student.email:
        raise PaymentError("Add a valid email address to your student record before paying.")
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaymentError("Online payment is not configured. Please contact the bursary.")

    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=student.pk)
            if Payment.objects.filter(student=student, term=term, status=Payment.Status.INITIALIZED).exists():
                raise PaymentError("A payment attempt is already in progress. Please wait for it to finish.")
            payment = Payment.objects.create(student=student, term=term, program=student.program, amount_kobo=schedule.total_kobo)
    except IntegrityError as error:
        raise PaymentError("A payment attempt is already in progress. Please wait for it to finish.") from error
    try:
        response = requests.post(
            f"{PAYSTACK_API}/transaction/initialize",
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"},
            json={"email": student.email, "amount": payment.amount_kobo, "reference": payment.reference, "callback_url": callback_url, "metadata": {"matric_number": student.matric_number, "term": str(term.pk)}},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("status") or not payload.get("data", {}).get("authorization_url"):
            raise PaymentError("Payment provider did not return a checkout link.")
        return payment, payload["data"]["authorization_url"]
    except (requests.RequestException, ValueError, PaymentError) as error:
        raise PaymentError("We could not start your payment. Please try again.") from error


def verify_paystack_reference(reference):
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaymentError("Payment verification is not configured.")
    try:
        response = requests.get(f"{PAYSTACK_API}/transaction/verify/{reference}", headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PaymentError("Payment verification is temporarily unavailable.") from error
    data = payload.get("data") or {}
    payment = Payment.objects.filter(reference=reference).select_related("student", "term__session").first()
    if not payment:
        raise PaymentError("No payment was found for this reference.")
    if data.get("reference") != payment.reference:
        raise PaymentError("The verified payment reference does not match this transaction.")
    if not payload.get("status") or data.get("status") != "success":
        if data.get("status") in {"failed", "abandoned", "reversed"}:
            Payment.objects.filter(reference=reference, status=Payment.Status.INITIALIZED).update(status=Payment.Status.FAILED, gateway_response={"status": data.get("status"), "reference": data.get("reference")})
        raise PaymentError("Payment has not been confirmed by the provider.")
    if data.get("amount") != payment.amount_kobo or data.get("currency") != "NGN":
        raise PaymentError("The verified payment does not match this transaction.")
    return confirm_payment(payment, data)


def confirm_payment(payment, gateway_data):
    if gateway_data.get("reference") != payment.reference or gateway_data.get("amount") != payment.amount_kobo or gateway_data.get("currency") != "NGN":
        raise PaymentError("The payment details do not match the expected amount.")
    paid_at_value = gateway_data.get("paid_at")
    try:
        paid_at = timezone.datetime.fromisoformat(paid_at_value.replace("Z", "+00:00")) if paid_at_value else timezone.now()
        if timezone.is_naive(paid_at):
            paid_at = paid_at.replace(tzinfo=datetime_timezone.utc)
    except (AttributeError, ValueError):
        paid_at = timezone.now()
    safe_response = {key: gateway_data.get(key) for key in ("id", "status", "reference", "amount", "currency", "channel", "paid_at", "gateway_response")}
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status == Payment.Status.PAID:
            return locked
        if Payment.objects.filter(student=locked.student, term=locked.term, status=Payment.Status.PAID).exclude(pk=locked.pk).exists():
            raise PaymentError("A different payment is already confirmed for this session and semester.")
        locked.status = Payment.Status.PAID
        locked.paid_at = paid_at
        locked.gateway_response = safe_response
        locked.sync_status = Payment.SyncStatus.PENDING
        locked.save(update_fields=["status", "paid_at", "gateway_response", "sync_status"])
        IntegrationOutbox.objects.get_or_create(payment=locked)
    return locked


def valid_paystack_signature(raw_body, signature):
    if not settings.PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def parse_webhook_event(raw_body):
    try:
        return json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise PaymentError("Invalid webhook JSON.") from error