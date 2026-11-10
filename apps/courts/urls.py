"""URL routes for the courts API (mounted under /api/v1/)."""

from datetime import date

from django.urls import path, register_converter

from .views import (
    ClubDetailView,
    ClubRegistrationView,
    CourtListView,
    CourtScheduleCopyView,
    CourtScheduleDetailView,
    CourtScheduleListView,
    DayTemplateDetailView,
    DayTemplateListView,
    ScheduleOverrideDetailView,
    ScheduleOverrideListView,
)


class IsoDateConverter:
    regex = r"\d{4}-\d{2}-\d{2}"

    def to_python(self, value):
        return date.fromisoformat(value)  # ValueError (e.g. 2030-02-30) -> 404

    def to_url(self, value):
        return value.isoformat() if isinstance(value, date) else value


register_converter(IsoDateConverter, "isodate")

app_name = "courts"

urlpatterns = [
    path("clubs/", ClubRegistrationView.as_view(), name="club-register"),
    path("clubs/<uuid:pk>/", ClubDetailView.as_view(), name="club-detail"),
    path("clubs/<uuid:club_pk>/day-templates/", DayTemplateListView.as_view(), name="day-template-list"),
    path("clubs/<uuid:club_pk>/day-templates/<uuid:pk>/", DayTemplateDetailView.as_view(), name="day-template-detail"),
    path("courts/", CourtListView.as_view(), name="court-list"),
    path("courts/<uuid:court_pk>/schedules/", CourtScheduleListView.as_view(), name="court-schedule-list"),
    path("courts/<uuid:court_pk>/schedules/copy/", CourtScheduleCopyView.as_view(), name="court-schedule-copy"),
    path("courts/<uuid:court_pk>/schedules/<uuid:pk>/", CourtScheduleDetailView.as_view(), name="court-schedule-detail"),
    path("courts/<uuid:court_pk>/overrides/", ScheduleOverrideListView.as_view(), name="schedule-override-list"),
    path("courts/<uuid:court_pk>/overrides/<isodate:day>/", ScheduleOverrideDetailView.as_view(), name="schedule-override-detail"),
]
