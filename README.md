# Postgraduate Course Registration Payments

A Django 5.2 portal for postgraduate students to pay course-registration fees, and for staff to manage students, academic terms, payment windows, fees, reports, and registration-portal clearance sync. PostgreSQL is the production database; SQLite is enabled for a quick local smoke test. Paystack is the default payment gateway.

## Included

- Student matric-number login, normalized to uppercase; Argon2 password hashing, forced first-login password change, five-attempt temporary lockout, and staff password reset.
- Staff user administration and Django admin screens for sessions, semesters, per-programme payment windows, fee schedules/line items, payment history, sync outbox, and immutable audit events.
- Paystack checkout amount determined from the published server-side fee schedule, server-side verify API check, signed webhook, duplicate-payment constraints, receipt PDF with QR reference, and payment history.
- Registration portal API adapter with configurable payload-field mapping, idempotency key, batched matric-number validation, retryable transactional outbox, and staff retry/report screens.
- CSV/XLSX student import preview, validation/error report, registration-portal warnings, upsert without resetting existing passwords, CSV template, single-student editing, deactivation, and CSV export.
- Staff reports with programme totals, paid/unpaid counts, transaction reconciliation, and CSV export.

## Run locally

Prerequisites: Python 3.12+, Docker Desktop with Compose, and a Paystack test account for checkout testing.

1. Create local configuration: `Copy-Item .env.example .env`. Set a new `DJANGO_SECRET_KEY`, `ADMIN_PASSWORD`, Paystack test keys, and `COURSE_REGISTRATION_URL`. Do not use sample credentials in a deployed environment.
2. Start services: `docker compose up --build`.
3. Open `http://localhost:8000`. In `DEBUG=true` only, the seed command creates the local-only `admin` / `admin` account and requires a password change at first sign-in. Change it immediately. Docker applies migrations and creates the initial `2026/2027` First Semester term plus unpublished sample fee schedules. Payment windows remain closed until an administrator opens them.
4. To create development sample students, set `SEED_SAMPLE_STUDENTS=true` in `.env` and restart the web service. The three sample matric numbers and initial passwords are listed in `portal/management/commands/seed_portal.py`; change these accounts before exposing a non-local deployment.
5. To create an administrator interactively instead, run `docker compose exec web python manage.py createsuperuser`.

The web process is at port 8000, PostgreSQL and Redis are private to the Compose network, and separate Celery worker/beat services deliver and retry portal syncs. Configure Paystack's webhook URL as `https://YOUR_HOST/payments/webhook/paystack/`.

## Local Python commands

Create and activate a virtual environment, install `requirements.txt`, and copy `.env.example` to `.env`. For a no-container smoke test, set `DATABASE_URL=sqlite:///db.sqlite3` and `DJANGO_DEBUG=true`; then run:

```powershell
python manage.py migrate
python manage.py seed_portal
python manage.py createsuperuser
python manage.py runserver
```

Run tests with `python manage.py test portal.tests`. Run `python manage.py check --deploy` against production environment settings before deployment. Production must use PostgreSQL, Redis, HTTPS at the proxy, a strong secret, restrictive hosts/origins, SMTP configuration, and real gateway/portal credentials.

## Staff setup

Sign in at `/django-admin/` with the administrator account. Create or select a session and semester under Academic terms and use **Set selected session and semester as active**. Configure one payment window per programme, publish a fee schedule with fee line items, and open the payment window. Existing payment records retain their original term, programme, amount, and reference if the active term or future fees change.

The staff operations dashboard is at `/staff/`; `/django-admin/` redirects staff there. It contains the active-term overview, academic session/semester controls, programme fee and payment-window forms, student CRUD and upload preview/commit, payment reconciliation, integration retry, and audit log. Detailed Django model editors remain at their direct `/django-admin/portal/.../` URLs. Server-side student lists and the full integration queue are paginated. Django groups/permissions can be assigned through the admin; a finance user should receive read-only view permissions and no add/change/delete permissions.

The downloadable CSV template is available from the student upload page. XLSX uploads use the first worksheet and the first row as headers. Required columns: `matric_number`, `surname`, `first_name`, `program`, `department`, `faculty`, and `level_year`. Optional columns: `other_names`, `email`, `phone`. Program values are `MSc`, `PGD`, or `PhD`. Duplicate matric values inside one upload block commit; an existing student is updated without changing their password unless the explicit reset checkbox is selected at commit.

## Configuration

See `.env.example` for the full list. Important production settings:

- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, and `SECURE_SSL_REDIRECT=true`.
- `DATABASE_URL` for PostgreSQL and `REDIS_URL` for Celery and shared login throttling.
- `PAYSTACK_SECRET_KEY` and `PAYSTACK_PUBLIC_KEY` from the correct Paystack environment (test vs live).
- For a local end-to-end checkout without gateway credentials, keep `DJANGO_DEBUG=true`, leave `PAYSTACK_SECRET_KEY` blank, and enable `DEMO_PAYMENTS=true`. The dashboard shows a separate demo checkout, marks the receipt as test-only, and does not transfer funds or send a registration clearance. Demo payments are disabled whenever `DEBUG` is false. To test the real gateway, configure Paystack test keys instead.
- `COURSE_REGISTRATION_URL`, `PORTAL_API_URL`, `PORTAL_API_TOKEN`, `PORTAL_FIELD_MAPPING`, and optionally `PORTAL_VERIFY_STUDENT_URL` / `PORTAL_VERIFY_STUDENT_TOKEN`.
- SMTP variables for successful-payment email or other institutional email notices. Without SMTP, Django uses console email.

Payment verification does not trust the browser callback. Paystack's response is checked for successful status, exact local reference, exact amount in kobo, and NGN currency. The webhook body is checked with Paystack's HMAC-SHA512 signature before processing. Keep the secret key server-side only.

## Integration contract

The adapter expects the existing registration portal to accept a JSON `POST` at `PORTAL_API_URL`. The application maps these logical fields to the portal's actual JSON keys using `PORTAL_FIELD_MAPPING`: `matric_number`, `session`, `semester`, `program`, `amount` (NGN decimal string), `payment_reference`, `payment_status` (`paid`), and `payment_date` (ISO-8601). It sends `Idempotency-Key: <payment_reference>` and bearer authentication when configured.

The receiver must atomically upsert its clearance record using a unique key on payment reference or `(matric_number, session, semester)`, commit before returning 2xx, and return a non-2xx response when the transaction fails. This is what makes retried HTTP delivery safe. The local outbox records attempts, backs off exponentially, surfaces terminal failures to staff, and permits manual retry. `PORTAL_VERIFY_STUDENT_URL` is a batch `POST` endpoint: request body `{"matric_numbers":[...]}`, response body `{"existing":["EXACT/MATRIC", ...]}`. An omitted URL produces a visible warning during import; an API outage is also shown as a warning, not a silent pass.

The real portal endpoint, authentication scheme, matching rules, and field names were not supplied, so the adapter is intentionally configurable and no registration-portal write is assumed successful until it returns 2xx. Before live payments, the portal owner must confirm and test this contract (or provide the existing portal's API/database schema so the adapter can be completed against it). Do not point this application directly at production tables without a least-privilege service identity and portal-owner-approved transaction/idempotency rules.

## Production checklist

- Deploy behind HTTPS; set secure environment secrets and rotate keys independently from source control.
- Use a dedicated production PostgreSQL database and Redis instance with persistence/monitoring, run migrations once, and keep Celery worker and beat running.
- Configure Paystack live keys and webhook URL; test successful, failed, duplicate, delayed, and amount-mismatch transactions in test mode first.
- Complete an end-to-end registration portal test, including repeated idempotency key delivery and failed-sync retry.
- Set the registration URL and fee schedules/windows, provision staff with least privilege, and change/remove all development sample accounts.
- Back up PostgreSQL and protect/download receipts and exports according to university retention policy.