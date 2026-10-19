from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.password_validation import validate_password

from .models import User


class MatricLoginForm(forms.Form):
    matric_number = forms.CharField(max_length=40)
    password = forms.CharField(widget=forms.PasswordInput)

    def clean_matric_number(self):
        return self.cleaned_data["matric_number"].strip().upper()


class FirstPasswordChangeForm(PasswordChangeForm):
    def clean_new_password1(self):
        password = self.cleaned_data["new_password1"]
        validate_password(password, self.user)
        return password


class StudentForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["matric_number", "surname", "first_name", "other_names", "program", "department", "faculty", "level_year", "email", "phone", "is_active"]


class UploadForm(forms.Form):
    file = forms.FileField()
