import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone


class User(AbstractUser):
    class Program(models.TextChoices):
        MSC = "MSc", "MSc"
        PGD = "PGD", "PGD"
        PHD = "PhD", "PhD"

    matric_number = models.CharField(max_length=40, unique=True, null=True, blank=True, db_index=True)
    surname = models.CharField(max_length=100, blank=True)
    first_name = models.CharField(max_length=100, blank=True)
    other_names = models.CharField(max_length=150, blank=True)
    program = models.CharField(max_length=3, choices=Program.choices, blank=True)
    department = models.CharField(max_length=150, blank=True)
    faculty = models.CharField(max_length=150, blank=True)
    level_year = models.CharField(max_length=30, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    must_change_password = models.BooleanField(default=False)
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if self.matric_number:
            self.matric_number = self.matric_number.strip().upper()
            self.username = self.matric_number
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return " ".join(part for part in (self.surname, self.first_name, self.other_names) if part).strip() or self.get_full_name()

    def __str__(self):
        return self.matric_number or self.username


class AcademicSession(models.Model):
    name = models.CharField(max_length=9, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-name"]

    def __str__(self):
        return self.name


class AcademicTerm(models.Model):
    class Semester(models.TextChoices):
        FIRST = "First", "First"
        SECOND = "Second", "Second"

    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="terms")
    semester = models.CharField(max_length=6, choices=Semester.choices)
    is_current = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "semester"], name="unique_session_semester"), models.UniqueConstraint(fields=["is_current"], condition=Q(is_current=True), name="one_current_term")]
        ordering = ["-session__name", "semester"]

    def __str__(self):
        return f"{self.session.name} · {self.semester} Semester"


class PaymentWindow(models.Model):
    term = models.ForeignKey(AcademicTerm, on_delete=models.CASCADE, related_name="windows")
    program = models.CharField(max_length=3, choices=User.Program.choices)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_open = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["term", "program"], name="unique_term_program_window")]

    @property
    def currently_open(self):
        now = timezone.now()
        return self.is_open and self.starts_at <= now <= self.ends_at


class FeeSchedule(models.Model):
    term = models.ForeignKey(AcademicTerm, on_delete=models.PROTECT, related_name="fee_schedules")
    program = models.CharField(max_length=3, choices=User.Program.choices)
    is_published = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["term", "program"], name="unique_fee_schedule")]

    @property
    def total_kobo(self):
        return sum(item.amount_kobo for item in self.items.all())

    @property
    def total_naira(self):
        return self.total_kobo / 100


class FeeLineItem(models.Model):
    schedule = models.ForeignKey(FeeSchedule, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=160)
    amount_kobo = models.PositiveBigIntegerField()

    @property
    def amount_naira(self):
        return self.amount_kobo / 100

    def __str__(self):
        return f"{self.description}: NGN {self.amount_kobo / 100:,.2f}"


class Payment(models.Model):
    class Status(models.TextChoices):
        INITIALIZED = "initialized", "Initialized"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    class SyncStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SYNCED = "synced", "Synced"
        FAILED = "failed", "Failed"
        DEMO = "demo", "Demo only"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(User, on_delete=models.PROTECT, related_name="payments")
    term = models.ForeignKey(AcademicTerm, on_delete=models.PROTECT, related_name="payments")
    program = models.CharField(max_length=3, choices=User.Program.choices)
    amount_kobo = models.PositiveBigIntegerField()
    reference = models.CharField(max_length=100, unique=True, default=uuid.uuid4)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INITIALIZED)
    sync_status = models.CharField(max_length=10, choices=SyncStatus.choices, default=SyncStatus.PENDING)
    gateway_response = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["student", "term"], condition=Q(status="paid"), name="one_paid_payment_per_student_term"),
            models.UniqueConstraint(fields=["student", "term"], condition=Q(status="initialized"), name="one_pending_payment_per_student_term"),
        ]

    @property
    def amount_naira(self):
        return self.amount_kobo / 100


class IntegrationOutbox(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        FAILED = "failed", "Failed"
        SYNCED = "synced", "Synced"

    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name="outbox")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class AuditEvent(models.Model):
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="audit_events")
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=180)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def display_action(self):
        return self.action.replace("_", " ").capitalize()


class StudentImport(models.Model):
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="student_imports")
    filename = models.CharField(max_length=255)
    rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    committed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]