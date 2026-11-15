"""URL configuration for CourtControl."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def healthcheck(_request):
    return JsonResponse({"status": "ok", "service": "courtcontrol"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", healthcheck, name="healthcheck"),
    path("api/v1/", include("apps.courts.urls")),
    path("api/v1/", include("apps.bookings.urls")),
    path("api/v1/", include("apps.users.urls")),
    # Session login/logout for any user (not just staff) + browsable API login link.
    path("api-auth/", include("rest_framework.urls")),
]
# Uploaded files on local disk (MEDIA_ROOT), e.g. player pictures in
# development. No-op unless DEBUG; in production a CDN/S3 serves them.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

admin.site.site_header = "CourtControl Administration"
admin.site.site_title = "CourtControl"
admin.site.index_title = "Tennis Court Control & Scheduling"
