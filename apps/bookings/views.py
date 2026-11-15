"""REST views for reservations."""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.courts.models import Club
from apps.courts.services import get_administered_clubs

from . import openapi
from .filters import ReservationFilter
from .models import BookingRule, Reservation
from .rules import describe_kinds
from .serializers import (
    BookingRuleSerializer,
    ReservationCancelSerializer,
    ReservationCreateSerializer,
    ReservationSerializer,
)
from .services import BookingError, CancellationError, cancel_reservation, create_reservation


@openapi.reservation_list
class ReservationListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/v1/reservations/ — the user's reservations (as booker or guest).
    POST /api/v1/reservations/ — book a court.

    GET filters (AND-combined; comma-separated values are OR-ed; dates/times
    are wall-clock in each club's timezone):
        state=upcoming,pending_payment,completed,cancelled,expired
        date=YYYY-MM-DD | date_from=... | date_to=...   (inclusive, open-ended)
        time_from=HH:MM  (starts at/after)   time_to=HH:MM  (ends at/before)
        player=<id or email>,...   (booker or guest)   role=owner|guest
        club=<uuid>,...   court=<uuid>,...   booking_type=SINGLES,DOUBLES,...
        ordering=start|-start|created|-created|price|-price   (default: -start)

    POST body (date/times are wall-clock in the court's club timezone):
        {"court": "<uuid>", "date": "2030-01-08", "start_time": "18:00",
         "end_time": "19:00", "booking_type": "SINGLES",
         "includes_lighting": true, "guests": ["friend@example.com", "<player id>"], "notes": ""}
    Guests: registered players by email or by player id (user id), mixed; max 3.

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
        if getattr(self, "swagger_fake_view", False):  # OpenAPI schema generation (no user)
            return Reservation.objects.none()
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


@openapi.reservation_cancel
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


# --- Booking rules (club admins and site staff; others get 404) -----------------------


class _ClubRulesView(APIView):
    """The club whose rules are managed: one the user administers (else 404)."""

    def get_club(self) -> Club:
        return get_object_or_404(get_administered_clubs(self.request.user), pk=self.kwargs["club_pk"])


@openapi.booking_rule_list
class BookingRuleListView(_ClubRulesView):
    """
    GET  /api/v1/clubs/<club>/booking-rules/ — the club's booking rules.
    POST /api/v1/clubs/<club>/booking-rules/ — add one:
         {"name": "Women's evening", "kind": "player_gender", "params": {"allowed": ["FEMALE"]},
          "courts": [], "weekdays": ["TUESDAY"], "start_time": "19:00", "end_time": "21:00"}

    Rules apply to bookings made after they're saved; existing bookings are
    never affected. 201 | 400 invalid kind/params/scope | 404 not your club.
    """

    def get(self, request, club_pk):
        club = self.get_club()
        rules = club.booking_rules.prefetch_related("courts")
        return Response(BookingRuleSerializer(rules, many=True, context={"club": club}).data)

    def post(self, request, club_pk):
        club = self.get_club()
        serializer = BookingRuleSerializer(data=request.data, context={"club": club})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@openapi.booking_rule_detail
class BookingRuleDetailView(_ClubRulesView):
    """
    GET    /api/v1/clubs/<club>/booking-rules/<id>/
    PATCH  — any subset of the fields; changing `kind` needs `params` valid for it.
             Set "is_active": false to pause a rule.
    DELETE — 204. Neither affects existing bookings.
    """

    def get_rule(self) -> tuple[Club, BookingRule]:
        club = self.get_club()
        return club, get_object_or_404(club.booking_rules.prefetch_related("courts"), pk=self.kwargs["pk"])

    def get(self, request, club_pk, pk):
        club, rule = self.get_rule()
        return Response(BookingRuleSerializer(rule, context={"club": club}).data)

    def patch(self, request, club_pk, pk):
        club, rule = self.get_rule()
        serializer = BookingRuleSerializer(rule, data=request.data, partial=True, context={"club": club})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, club_pk, pk):
        _club, rule = self.get_rule()
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@openapi.booking_rule_kinds
class BookingRuleKindsView(APIView):
    """
    GET /api/v1/booking-rule-kinds/ — every rule kind and its params (type,
    required, default, choices, limits, help), for building rule editors.
    """

    def get(self, request):
        return Response(describe_kinds())
