import requests
from django.conf import settings

from portal.models import Payment


class PortalIntegrationError(Exception):
    pass


class RegistrationPortalAdapter:
    def _headers(self, idempotency_key=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if settings.PORTAL_API_TOKEN:
            headers["Authorization"] = f"Bearer {settings.PORTAL_API_TOKEN}"
        elif settings.PORTAL_VERIFY_STUDENT_TOKEN:
            headers["Authorization"] = f"Bearer {settings.PORTAL_VERIFY_STUDENT_TOKEN}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def verify_students(self, matric_numbers):
        if not settings.PORTAL_VERIFY_STUDENT_URL:
            return None
        if not matric_numbers:
            return set()
        try:
            headers = self._headers()
            if settings.PORTAL_VERIFY_STUDENT_TOKEN:
                headers["Authorization"] = f"Bearer {settings.PORTAL_VERIFY_STUDENT_TOKEN}"
            response = requests.post(settings.PORTAL_VERIFY_STUDENT_URL, json={"matric_numbers": matric_numbers}, headers=headers, timeout=15)
            response.raise_for_status()
            result = response.json()
            existing = result.get("existing")
            if not isinstance(existing, list):
                raise ValueError("Expected an 'existing' list in portal response.")
            return {str(matric).strip().upper() for matric in existing}
        except (requests.RequestException, ValueError) as error:
            raise PortalIntegrationError("The registration portal could not verify this matric number.") from error

    def sync_payment(self, payment: Payment):
        if not settings.PORTAL_API_URL:
            raise PortalIntegrationError("Set PORTAL_API_URL to enable payment clearance sync.")
        values = {
            "matric_number": payment.student.matric_number,
            "session": payment.term.session.name,
            "semester": payment.term.semester,
            "program": payment.program,
            "amount": str(payment.amount_naira),
            "payment_reference": payment.reference,
            "payment_status": "paid",
            "payment_date": payment.paid_at.isoformat() if payment.paid_at else "",
        }
        payload = {settings.PORTAL_FIELD_MAPPING.get(key, key): value for key, value in values.items()}
        try:
            response = requests.post(settings.PORTAL_API_URL, json=payload, headers=self._headers(payment.reference), timeout=15)
            response.raise_for_status()
        except requests.RequestException as error:
            raise PortalIntegrationError(f"Registration portal sync failed: {error}") from error