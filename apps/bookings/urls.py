"""URL routes for the reservations API (mounted under /api/v1/)."""

from django.urls import path

from .views import ReservationCancelView, ReservationListCreateView

app_name = "bookings"

urlpatterns = [
    path("reservations/", ReservationListCreateView.as_view(), name="reservation-list"),
    path(
        "reservations/<uuid:pk>/cancel/",
        ReservationCancelView.as_view(),
        name="reservation-cancel",
    ),
]
