"""REST views for reservations."""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, status
from rest_framework.response import Response

from .filters import ReservationFilter
from .models import Reservation
from .serializers import (
    ReservationCancelSerializer,
    ReservationCreateSerializer,
    ReservationSerializer,
)
from .services import BookingError, CancellationError, cancel_reservation, create_reservation


class ReservationListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/v1/reservations/ — the user's reservations (as booker or guest).
    POST /api/v1/reservations/ — book a court.

    GET filters (AND-combined; comma-separated values are OR-ed; dates/times
    are wall-clock in each club's timezone):
        state=upcoming,pending_payment,completed,cancelled,expired
        date=YYYY-MM-DD | date_from=... | date_to=...   (inclusive, open-ended)
        time_from=HH:MM  (starts at/after)   time_to=HH:MM  (ends at/before)
        player=<email>,<email>   (booker or guest)   role=owner|guest
        club=<uuid>,...   court=<uuid>,...   booking_type=SINGLES,DOUBLES,...
        ordering=start|-start|created|-created|price|-price   (default: -start)

    POST body (date/times are wall-clock in the court's club timezone):
        {"court": "<uuid>", "date": "2030-01-08", "start_time": "18:00",
         "end_time": "19:00", "booking_type": "SINGLES",
         "includes_lighting": true, "guests": ["friend@example.com"], "notes": ""}

    Payment isn't implemented yet, so bookings are CONFIRMED immediately
    (a PENDING_PAYMENT hold would be expired by the beat task after
    RESERVATION_HOLD_MINUTES).

    POST: 201 reservation | 400 invalid request or slot not bookable |
          409 slot already taken (including a concurrent booking race).
    """

    filter_backends = [DjangoFilterBackend]
    filterset_class = ReservationFilter

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ReservationCreateSerializer
        return ReservationSerializer

    def get_queryset(self):
        return (
            Reservation.objects.involving(self.request.user)
            .with_state(timezone.now())
            .with_local_times()
            .select_related("court__club", "primary_player")
            .prefetch_related("guests")
            .order_by("-start_datetime", "-created_at")
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            reservation = create_reservation(
                court=data["court"],
                primary_player=request.user,
                start_datetime=data["start_datetime"],
                end_datetime=data["end_datetime"],
                booking_type=data["booking_type"],
                includes_lighting=data["includes_lighting"],
                guests=data["guests"],
                notes=data["notes"],
                status=Reservation.Status.CONFIRMED,
            )
        except BookingError as exc:
            return Response(
                {"non_field_errors": exc.errors},
                status=status.HTTP_409_CONFLICT if exc.conflict else status.HTTP_400_BAD_REQUEST,
            )

        reservation = self.get_queryset().get(pk=reservation.pk)
        return Response(
            ReservationSerializer(reservation, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class ReservationCancelView(generics.GenericAPIView):
    """
    POST /api/v1/reservations/{id}/cancel/   body (optional): {"reason": "..."}

    Cancels a booking per the club's policy (Club.cancellation_window_hours,
    default 24): confirmed bookings until `start - window`, unpaid holds until
    the start. Only the player who booked can cancel. Idempotent: repeating the
    call on a booking you cancelled returns 200 with no further effect.

    200 cancelled reservation | 403 you're a guest in this booking |
    404 not found / not yours | 409 can't be cancelled (deadline passed,
    already started, expired, completed).
    """

    serializer_class = ReservationCancelSerializer

    def get_queryset(self):
        return ReservationListCreateView.get_queryset(self)

    def post(self, request, pk):
        reservation = get_object_or_404(self.get_queryset(), pk=pk)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            cancel_reservation(
                reservation, user=request.user, reason=serializer.validated_data["reason"]
            )
        except CancellationError as exc:
            code = (
                status.HTTP_403_FORBIDDEN
                if exc.code == "forbidden"
                else status.HTTP_409_CONFLICT
            )
            return Response({"detail": str(exc)}, status=code)

        reservation = self.get_queryset().get(pk=pk)
        return Response(
            ReservationSerializer(reservation, context=self.get_serializer_context()).data
        )
