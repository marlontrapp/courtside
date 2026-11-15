"""URL routes for the reservations API (mounted under /api/v1/)."""

from django.urls import path

from .views import (
    BookingRuleDetailView,
    BookingRuleKindsView,
    BookingRuleListView,
    ReservationCancelView,
    ReservationListCreateView,
)

app_name = "bookings"

urlpatterns = [
    path("reservations/", ReservationListCreateView.as_view(), name="reservation-list"),
    path(
        "reservations/<uuid:pk>/cancel/",
        ReservationCancelView.as_view(),
        name="reservation-cancel",
    ),
    path("clubs/<uuid:club_pk>/booking-rules/", BookingRuleListView.as_view(), name="booking-rule-list"),
    path("clubs/<uuid:club_pk>/booking-rules/<uuid:pk>/", BookingRuleDetailView.as_view(), name="booking-rule-detail"),
    path("booking-rule-kinds/", BookingRuleKindsView.as_view(), name="booking-rule-kinds"),
]
