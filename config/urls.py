"""URL configuration for CourtControl."""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import path


def healthcheck(_request):
    return JsonResponse({"status": "ok", "service": "courtcontrol"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", healthcheck, name="healthcheck"),
]

admin.site.site_header = "CourtControl Administration"
admin.site.site_title = "CourtControl"
admin.site.index_title = "Tennis Court Control & Scheduling"
