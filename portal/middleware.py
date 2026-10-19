from django.shortcuts import redirect
from django.urls import reverse


class PasswordChangeRequiredMiddleware:
    exempt_names = {"login", "logout", "password_change", "password_change_done", "webhook"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and getattr(user, "must_change_password", False):
            if request.resolver_match and request.resolver_match.url_name not in self.exempt_names:
                return redirect(reverse("password_change"))
        return self.get_response(request)