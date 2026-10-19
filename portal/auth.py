from datetime import timedelta

from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from .models import User

_DUMMY_PASSWORD = make_password("unusable-dummy-password")


class MatricAuthenticationBackend(ModelBackend):
    max_attempts = 5
    lock_duration = timedelta(minutes=15)

    def authenticate(self, request, username=None, password=None, **kwargs):
        matric = (username or kwargs.get("matric_number") or "").strip().upper()
        if not matric or password is None:
            return None
        try:
            user = User.objects.get(matric_number=matric)
        except User.DoesNotExist:
            try:
                user = User.objects.get(username=username, is_staff=True)
            except User.DoesNotExist:
                check_password(password, _DUMMY_PASSWORD)
                return None
        if user.locked_until and user.locked_until > timezone.now():
            check_password(password, user.password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            User.objects.filter(pk=user.pk).update(failed_login_attempts=0, locked_until=None)
            return user
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=user.pk)
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= self.max_attempts:
                user.locked_until = timezone.now() + self.lock_duration
                user.failed_login_attempts = 0
            user.save(update_fields=["failed_login_attempts", "locked_until"])
        return None