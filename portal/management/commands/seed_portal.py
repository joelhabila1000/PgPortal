import os
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from portal.models import AcademicSession, AcademicTerm, FeeLineItem, FeeSchedule, PaymentWindow, User


class Command(BaseCommand):
    help = "Create an initial academic term, sample fee schedules, and optional development students."

    def handle(self, *args, **options):
        session, _ = AcademicSession.objects.get_or_create(name="2026/2027")
        term, _ = AcademicTerm.objects.get_or_create(session=session, semester=AcademicTerm.Semester.FIRST)
        if not AcademicTerm.objects.filter(is_current=True).exists():
            term.is_current = True
            term.save(update_fields=["is_current"])
        fees = {"MSc": (55000, 12000, 3000), "PGD": (45000, 10000, 3000), "PhD": (70000, 15000, 5000)}
        for program, (registration, portal, processing) in fees.items():
            schedule, _ = FeeSchedule.objects.get_or_create(term=term, program=program)
            if not schedule.items.exists():
                for description, amount in (("Course registration fee", registration), ("Portal access fee", portal), ("Processing fee", processing)):
                    FeeLineItem.objects.create(schedule=schedule, description=description, amount_kobo=amount * 100)
            if settings.SEED_SAMPLE_STUDENTS and not schedule.is_published:
                schedule.is_published = True
                schedule.save(update_fields=["is_published"])
            window, _ = PaymentWindow.objects.get_or_create(term=term, program=program, defaults={"starts_at": timezone.now(), "ends_at": timezone.now() + timedelta(days=90), "is_open": False})
            if settings.DEBUG and settings.SEED_SAMPLE_STUDENTS:
                window.starts_at = timezone.now() - timedelta(minutes=1)
                window.ends_at = timezone.now() + timedelta(days=90)
                window.is_open = True
                window.save(update_fields=["starts_at", "ends_at", "is_open"])
        email = os.environ.get("ADMIN_EMAIL", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if settings.DEBUG and not User.objects.filter(username="admin").exists():
            admin = User.objects.create_superuser(username="admin", email=email or "admin@localhost", password="admin")
            admin.must_change_password = True
            admin.save(update_fields=["must_change_password"])
            self.stdout.write(self.style.WARNING("Created local-only admin/admin account. Change its password at first sign-in."))
        elif email and password and password != "change-this-before-deploy" and not User.objects.filter(username="admin").exists():
            User.objects.create_superuser(username="admin", email=email, password=password)
            self.stdout.write(self.style.SUCCESS("Created the admin account from environment variables."))
        elif not User.objects.filter(is_superuser=True).exists():
            self.stdout.write(self.style.WARNING("No admin account exists. Run `python manage.py createsuperuser` after setup."))
        if settings.SEED_SAMPLE_STUDENTS:
            samples = [
                ("PG/2026/001", "ADEYEMI", "TOLA", "", "MSc", "Computer Science"),
                ("PG/2026/002", "OKAFOR", "CHIDI", "EMEKA", "PGD", "Public Administration"),
                ("PG/2026/003", "IBRAHIM", "AISHA", "", "PhD", "Economics"),
            ]
            for matric, surname, first, other, program, department in samples:
                student, created = User.objects.get_or_create(matric_number=matric, defaults={"surname": surname, "first_name": first, "other_names": other, "program": program, "department": department, "faculty": "Postgraduate School", "level_year": "Year 1", "email": f"{matric.lower().replace('/', '.') }@example.edu.ng", "must_change_password": True})
                if created:
                    student.set_password(matric)
                    student.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS("Development sample students are available; passwords equal their matric numbers."))
        if settings.DEBUG and settings.SEED_SAMPLE_STUDENTS:
            self.stdout.write(self.style.SUCCESS(f"Seeded {term}. Sample fees are published and programme payment windows are open for local testing."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Seeded academic term {term}. Payment windows remain closed until opened by an admin."))