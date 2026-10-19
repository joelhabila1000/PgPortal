# Existing Registration Portal Integration

## Required configuration

Set `PORTAL_API_URL` to the existing portal's payment-clearance endpoint and set `PORTAL_API_TOKEN` to a dedicated service credential. Configure `PORTAL_FIELD_MAPPING` as a JSON object that maps the application's logical names to accepted request JSON keys. For example:

```json
{"matric_number":"student_id","session":"academic_session","semester":"semester_name","program":"programme","amount":"paid_amount","payment_reference":"transaction_ref","payment_status":"status","payment_date":"paid_on"}
```

Set `COURSE_REGISTRATION_URL` to the actual portal destination shown to students after successful clearance sync.

Optionally configure `PORTAL_VERIFY_STUDENT_URL` and `PORTAL_VERIFY_STUDENT_TOKEN`. The verification endpoint accepts a batch of exact matric numbers and returns the subset that exists. Matric numbers are trimmed and uppercased by this portal before verification.

## Write behavior

For each verified Paystack payment, this portal stores an outbox row in the same local transaction as the successful payment state. A Celery worker sends an HTTP POST containing the mapped student, term, amount, reference, status, and date. The request carries `Idempotency-Key` equal to the Paystack reference. Transient failures retry with exponential backoff; repeated failure is visible in the staff sync queue and can be retried manually.

The registration API must implement the database transaction on its own side. It must validate the service credential, match the matric number exactly, atomically insert/update the clearance row, enforce a unique key on payment reference (and preferably student/session/semester), and return 2xx only after commit. Repeating the same request must update/return the same clearance row, never duplicate it. Do not return 2xx when a write only entered an unmonitored queue unless the receiver guarantees durable idempotent processing.

## Before enabling production

The university must provide the real endpoint URL, request/response examples, authentication/rotation process, exact session and semester values, amount units, and the registration portal's idempotency/transaction guarantees. Test exact matric matching, a normal payment, repeated webhook delivery, repeated API delivery, network timeout, portal outage and manual retry, and confirm that the student cannot register before the clearance row commits. Until those details are confirmed, portal sync remains explicitly pending/failed rather than being represented as successful.