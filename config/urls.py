from django.contrib import admin
from django.urls import include, path

from portal.views import admin_portal

urlpatterns = [path("django-admin/", admin_portal, name="admin_portal"), path("django-admin/", admin.site.urls), path("", include("portal.urls"))]