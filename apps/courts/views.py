"""REST views for clubs, courts and their schedules."""

from zoneinfo import ZoneInfo

from django.http import Http404
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.bookings.availability import get_courts_slots
from apps.common.pagination import NoCountPageNumberPagination
from apps.common.search import SearchQueryError
from apps.users.serializers import PlayerSummarySerializer

from . import openapi
from .filters import CourtFilter
from .models import Club, Court, CourtSchedule, DayTemplate, ScheduleOverride
from .schedule import club_today
from .search import search_clubs
from .serializers import (
    WINDOW_PARAMS,
    AvailabilityQuerySerializer,
    ClubRegistrationSerializer,
    ClubRejectSerializer,
    ClubReviewSerializer,
    ClubSearchResultSerializer,
    ClubSerializer,
    ClubUpdateSerializer,
    CopyScheduleSerializer,
    CourtAvailabilitySerializer,
    CourtScheduleSerializer,
    CourtScheduleWriteSerializer,
    CourtSerializer,
    DayTemplateSerializer,
    DayTemplateWriteSerializer,
    OverrideRangeSerializer,
    ScheduleOverrideSerializer,
    ScheduleOverrideWriteSerializer,
)
from .services import (
    ClubRegistrationLimit,
    ClubReviewError,
    ScheduleChange,
    approve_club,
    create_court,
    delete_court,
    get_visible_club,
    update_court,
    reject_club,
    ScheduleConflict,
    ScheduleError,
    LinkedToRules,
    ScheduleInUse,
    copy_schedule,
    create_day_template,
    delete_day_template,
    delete_override,
    delete_schedule,
    get_administered_clubs,
    get_administered_courts,
    get_bookable_courts,
    plan_schedule,
    register_club,
    set_override,
    update_day_template,
)


@openapi.club_registration
class ClubRegistrationView(generics.CreateAPIView):
    """
    POST /api/v1/clubs/ — register a club.

    Body: {"name": "Ibirapuera Tennis Center", "address": "Av. Pedro Álvares Cabral, São Paulo"}

    Only the name and address are taken; other settings (timezone, access
    type, cancellation window, ...) start at their defaults and are set later
    by editing the club. Names needn't be unique: when the name's slug is
    taken, the address is added to it, then a number.

    201 created club | 400 missing/blank name or address.
    """

    serializer_class = ClubRegistrationSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            club = register_club(**serializer.validated_data, registered_by=request.user)
        except ClubRegistrationLimit as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        return Response(
            ClubSerializer(club, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


@openapi.club_search
class ClubSearchView(generics.ListAPIView):
    """
    GET /api/v1/clubs/search/?q=<words> — find active clubs by name and address.

    Every word must appear in the club's name or address (street, city,
    country, ...), whole or partial, ignoring case and accents. Open and
    members-only clubs are both listed (`access_type` tells them apart).
    Best matches first — see apps/courts/search.py.

    Paginated without a total count: {"next", "previous", "results"}, ?page=N.

    200 results (possibly empty) | 400 missing/blank `q`, too long, too many
    words, or no word of 3+ letters/digits | 403 not logged in.
    """

    serializer_class = ClubSearchResultSerializer
    pagination_class = NoCountPageNumberPagination
    filter_backends = []  # no ?search= / ?ordering= from the project defaults

    def get_queryset(self):
        try:
            return search_clubs(self.request.query_params.get("q", ""))
        except SearchQueryError as exc:
            raise ValidationError({"q": [str(exc)]}) from exc


@openapi.club_detail
class ClubDetailView(generics.RetrieveUpdateAPIView):
    """
    GET   /api/v1/clubs/<uuid>/ — club detail: approved clubs for any logged-in
          user; a pending / rejected one only for its club admins and site staff.
    PATCH /api/v1/clubs/<uuid>/ — edit a club: site staff and the club's admins.

    Every setting is editable except `name` and `address`: changing those is
    a support request (the name anchors the slug and both may be referenced
    outside this API), rejected here with a 400 pointing at support.

    Body (any subset): {"timezone": "Europe/Lisbon", "access_type": "MEMBERS_ONLY",
                         "cancellation_window_hours": 12, "is_active": true}

    200 updated club | 400 invalid value, or name/address included |
    403 not staff nor a club admin | 404 unknown club (or not visible).
    """

    http_method_names = ["get", "patch", "head", "options"]

    def get_serializer_class(self):
        return ClubSerializer if self.request.method == "GET" else ClubUpdateSerializer

    def get_object(self) -> Club:
        club = get_visible_club(self.request.user, self.kwargs["pk"])
        if club is None:
            raise Http404
        return club

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not get_administered_clubs(request.user).filter(pk=instance.pk).exists():
            raise PermissionDenied("Only site staff and the club's admins can edit it.")
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ClubSerializer(instance, context=self.get_serializer_context()).data)


# --- Club review (site staff) -----------------------------------------------------------


@openapi.club_pending
class ClubPendingListView(generics.ListAPIView):
    """GET /api/v1/clubs/pending/ — clubs awaiting approval, oldest first. Site staff only."""

    serializer_class = ClubReviewSerializer
    permission_classes = [IsAdminUser]
    filter_backends = []

    def get_queryset(self):
        return (
            Club.objects.filter(approval_status=Club.ApprovalStatus.PENDING)
            .select_related("registered_by").order_by("created_at", "pk")
        )


class _ClubReviewView(generics.GenericAPIView):
    permission_classes = [IsAdminUser]
    queryset = Club.objects.all()

    def respond(self, review):
        try:
            club = review(self.get_object())
        except ClubReviewError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ClubReviewSerializer(club, context=self.get_serializer_context()).data)


@openapi.club_approve
class ClubApproveView(_ClubReviewView):
    """POST /api/v1/clubs/<uuid>/approve/ — make a pending (or rejected) club live. Site staff only."""

    serializer_class = ClubReviewSerializer

    def post(self, request, pk):
        return self.respond(lambda club: approve_club(club, by=request.user))


@openapi.club_reject
class ClubRejectView(_ClubReviewView):
    """POST /api/v1/clubs/<uuid>/reject/ {"reason": "…"} — reject a pending club. Site staff only."""

    serializer_class = ClubRejectSerializer

    def post(self, request, pk):
        serializer = ClubRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(lambda club: reject_club(club, by=request.user, reason=serializer.validated_data["reason"]))


# --- Schedules (club admins and site staff; others get 404) -----------------------------


def _reservation_summary(reservation) -> dict:
    tz = ZoneInfo(reservation.court.club.timezone)
    return {
        "id": str(reservation.pk),
        "court": str(reservation.court_id),
        "court_name": reservation.court.name,
        "start": reservation.start_datetime.astimezone(tz).isoformat(),
        "end": reservation.end_datetime.astimezone(tz).isoformat(),
        "primary_player": PlayerSummarySerializer(reservation.primary_player).data,  # never the email
        "status": reservation.status,
    }


def _rule_summary(rule) -> dict:
    return {"id": str(rule.pk), "name": rule.name}


def _unlink_summary(report) -> dict:
    """Booking rules a forced deletion unlinked, and those deactivated rather than widened."""
    return {
        "unlinked_booking_rules": [_rule_summary(rule) for rule in report.unlinked] if report else [],
        "deactivated_booking_rules": [_rule_summary(rule) for rule in report.deactivated] if report else [],
    }


def _force_param(request) -> bool:
    return str(request.query_params.get("force", "")).lower() in ("1", "true", "yes")


class ScheduleAPIView(APIView):
    """Maps schedule service errors to responses."""

    def handle_exception(self, exc):
        if isinstance(exc, ScheduleConflict):
            return Response(
                {
                    "detail": str(exc),
                    "conflicting_reservations": [_reservation_summary(r) for r in exc.reservations],
                    "hint": exc.hint,
                },
                status=status.HTTP_409_CONFLICT,
            )
        if isinstance(exc, LinkedToRules):
            return Response(
                {
                    "detail": exc.errors[0],
                    "booking_rules": [_rule_summary(rule) for rule in exc.rules],
                    "hint": "Unlink it from the rule(s), or send force=true: the links are removed, and a rule "
                            "left without any link of that kind is deactivated (rather than covering more).",
                },
                status=status.HTTP_409_CONFLICT,
            )
        if isinstance(exc, ScheduleInUse):
            return Response({"detail": exc.errors[0]}, status=status.HTTP_409_CONFLICT)
        if isinstance(exc, ScheduleError):
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)

    def get_club(self) -> Club:
        return get_object_or_404(get_administered_clubs(self.request.user), pk=self.kwargs["club_pk"])

    def get_court(self) -> Court:
        return get_object_or_404(
            get_administered_courts(self.request.user).select_related("club"), pk=self.kwargs["court_pk"]
        )

    @staticmethod
    def with_cancelled(data, change: ScheduleChange) -> dict:
        data = {**data, "cancelled_reservations": [_reservation_summary(r) for r in change.cancelled]}
        return {**data, **_unlink_summary(change.rules)} if change.rules else data


@openapi.court_manage_list
class ClubCourtListView(ScheduleAPIView):
    """
    GET  /api/v1/clubs/<club>/courts/ — every court of the club (inactive too).
    POST /api/v1/clubs/<club>/courts/ — add one. Club admins and site staff
         (pending clubs too: set them up before approval); others get 404.
    """

    def get(self, request, club_pk):
        return Response(CourtSerializer(self.get_club().courts.all(), many=True).data)

    def post(self, request, club_pk):
        club = self.get_club()
        serializer = CourtSerializer(data=request.data, context={"club": club})
        serializer.is_valid(raise_exception=True)
        data = {k: v for k, v in serializer.validated_data.items() if k != "force"}
        return Response(CourtSerializer(create_court(club, **data)).data, status=status.HTTP_201_CREATED)


@openapi.court_manage_detail
class ClubCourtDetailView(ScheduleAPIView):
    """
    GET    /api/v1/clubs/<club>/courts/<id>/
    PATCH  — any subset of the settings; deactivating a court with future
             bookings: 409 listing them, or "force": true to cancel them.
    DELETE — only courts without bookings (else 409: deactivate instead);
             linked to booking rules: 409, or ?force=true to unlink them.
    """

    def get_court_of_club(self) -> Court:
        return get_object_or_404(self.get_club().courts.select_related("club"), pk=self.kwargs["pk"])

    def get(self, request, club_pk, pk):
        return Response(CourtSerializer(self.get_court_of_club()).data)

    def patch(self, request, club_pk, pk):
        court = self.get_court_of_club()
        serializer = CourtSerializer(court, data=request.data, partial=True, context={"club": court.club})
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        force = data.pop("force", False)
        change = update_court(court, data, force=force)
        return Response(self.with_cancelled(CourtSerializer(change.result).data, change))

    def delete(self, request, club_pk, pk):
        force = _force_param(request)
        report = delete_court(self.get_court_of_club(), force=force)
        if not force:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(_unlink_summary(report))


@openapi.day_template_list
class DayTemplateListView(ScheduleAPIView):
    """
    GET  /api/v1/clubs/<club>/day-templates/ — the club's day templates.
    POST /api/v1/clubs/<club>/day-templates/ — {"name": "Workdays", "slots":
         [{"start": "06:00", "end": "07:00"}, ...]}; "24:00" ends a slot at midnight.
    """

    def get(self, request, club_pk):
        templates = self.get_club().day_templates.prefetch_related("slots").select_related("club")
        return Response(DayTemplateSerializer(templates, many=True).data)

    def post(self, request, club_pk):
        club = self.get_club()
        serializer = DayTemplateWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = create_day_template(
            club=club, name=serializer.validated_data["name"], slots=serializer.validated_data["slots"]
        )
        return Response(DayTemplateSerializer(template).data, status=status.HTTP_201_CREATED)


@openapi.day_template_detail
class DayTemplateDetailView(ScheduleAPIView):
    """
    GET    /api/v1/clubs/<club>/day-templates/<id>/
    PATCH  — {"name": ...} anytime; {"slots": [...]} only until the template has
             been in effect (409 after that). Future bookings that no longer fit:
             409, or cancelled with "force": true.
    DELETE — only when no schedule or override uses it (409 otherwise). Linked to
             booking rules (itself or its slots): 409 listing them, or ?force=true to
             unlink them (a rule left without links of that kind is deactivated) —
             200 {"unlinked_booking_rules", "deactivated_booking_rules"}.
    PATCH slots removing a slot linked to booking rules: 409, or "force": true (same).
    """

    def get_template(self) -> DayTemplate:
        return get_object_or_404(self.get_club().day_templates.select_related("club"), pk=self.kwargs["pk"])

    def get(self, request, club_pk, pk):
        return Response(DayTemplateSerializer(self.get_template()).data)

    def patch(self, request, club_pk, pk):
        template = self.get_template()
        serializer = DayTemplateWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        change = update_day_template(
            template, name=data.get("name"), slots=data.get("slots"), force=data.get("force", False)
        )
        template.refresh_from_db()
        return Response(self.with_cancelled(DayTemplateSerializer(template).data, change))

    def delete(self, request, club_pk, pk):
        force = _force_param(request)
        report = delete_day_template(self.get_template(), force=force)
        if not force:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(_unlink_summary(report))


@openapi.court_schedule_list
class CourtScheduleListView(ScheduleAPIView):
    """
    GET  /api/v1/courts/<court>/schedules/ — every version of the court's weekly
         schedule (past, current, planned), oldest first.
    POST /api/v1/courts/<court>/schedules/ — a new version from valid_from
         (today or later):
         {"valid_from": "2030-03-01", "valid_until": null,
          "days": [{"weekdays": ["MONDAY", ..., "FRIDAY"], "day_template": "<id>"},
                   {"weekdays": ["SUNDAY"], "day_template": null}],
          "force": false}
         Weekdays not sent keep the template they have on valid_from; null closes
         the day. Without valid_until it lasts until the next planned version.
    """

    def get(self, request, court_pk):
        versions = self.get_court().schedules.select_related("court__club").prefetch_related("days__day_template")
        return Response(CourtScheduleSerializer(versions, many=True).data)

    def post(self, request, court_pk):
        court = self.get_court()
        serializer = CourtScheduleWriteSerializer(data=request.data, context={"club": court.club})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        change = plan_schedule(
            court, valid_from=data["valid_from"], valid_until=data["valid_until"],
            days=data["days"], force=data["force"],
        )
        return Response(
            self.with_cancelled(CourtScheduleSerializer(change.result).data, change),
            status=status.HTTP_201_CREATED,
        )


@openapi.court_schedule_detail
class CourtScheduleDetailView(ScheduleAPIView):
    """
    GET    /api/v1/courts/<court>/schedules/<id>/
    DELETE /api/v1/courts/<court>/schedules/<id>/?force=true — planned versions
           only; the version before it extends over the freed period.
    """

    def get_version(self) -> CourtSchedule:
        return get_object_or_404(
            self.get_court().schedules.select_related("court__club"), pk=self.kwargs["pk"]
        )

    def get(self, request, court_pk, pk):
        return Response(CourtScheduleSerializer(self.get_version()).data)

    def delete(self, request, court_pk, pk):
        change = delete_schedule(self.get_version(), force=_force_param(request))
        return Response(self.with_cancelled({}, change))


@openapi.court_schedule_copy
class CourtScheduleCopyView(ScheduleAPIView):
    """
    POST /api/v1/courts/<court>/schedules/copy/ — give other courts of the same
    club the weekly schedule this court has on valid_from:
    {"courts": ["<id>", ...], "valid_from": "2030-03-01", "valid_until": null, "force": false}
    All-or-nothing. One-time overrides aren't copied.
    """

    def post(self, request, court_pk):
        source = self.get_court()
        serializer = CopyScheduleSerializer(
            data=request.data, context={"courts": get_administered_courts(request.user)}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        change = copy_schedule(
            source, data["courts"], valid_from=data["valid_from"],
            valid_until=data["valid_until"], force=data["force"],
        )
        schedules = [{"court": str(v.court_id), **CourtScheduleSerializer(v).data} for v in change.result]
        return Response(self.with_cancelled({"schedules": schedules}, change), status=status.HTTP_201_CREATED)


@openapi.override_list
class ScheduleOverrideListView(ScheduleAPIView):
    """GET /api/v1/courts/<court>/overrides/?date_from=&date_to= (default: from today on)."""

    def get(self, request, court_pk):
        court = self.get_court()
        query = OverrideRangeSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        date_from = query.validated_data.get("date_from") or club_today(court.club)
        overrides = court.schedule_overrides.filter(date__gte=date_from)
        if query.validated_data.get("date_to"):
            overrides = overrides.filter(date__lte=query.validated_data["date_to"])
        overrides = overrides.select_related("day_template").prefetch_related("day_template__slots")
        return Response(ScheduleOverrideSerializer(overrides, many=True).data)


@openapi.override_detail
class ScheduleOverrideDetailView(ScheduleAPIView):
    """
    GET    /api/v1/courts/<court>/overrides/<YYYY-MM-DD>/
    PUT    — {"day_template": "<id>" | null (closed), "type": "TOURNAMENT",
              "notes": "Junior club championship until 14:00", "force": false};
             today or later. Creates or replaces the override for that date. The
             type and notes are shown to members (courts listing notices,
             booking errors). type: MAINTENANCE | WEATHER | TOURNAMENT | EVENT |
             HOLIDAY | OTHER.
    DELETE — ?force=true; back to the weekly schedule (today or later).
    """

    def get_override(self, court) -> ScheduleOverride:
        return get_object_or_404(
            court.schedule_overrides.select_related("day_template", "court__club"), date=self.kwargs["day"]
        )

    def get(self, request, court_pk, day):
        return Response(ScheduleOverrideSerializer(self.get_override(self.get_court())).data)

    def put(self, request, court_pk, day):
        court = self.get_court()
        serializer = ScheduleOverrideWriteSerializer(data=request.data, context={"club": court.club})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        change = set_override(
            court, day=day, day_template=data["day_template"], type=data["type"], notes=data["notes"],
            force=data["force"],
        )
        return Response(self.with_cancelled(ScheduleOverrideSerializer(change.result).data, change))

    def delete(self, request, court_pk, day):
        change = delete_override(self.get_override(self.get_court()), force=_force_param(request))
        return Response(self.with_cancelled({}, change))


@openapi.court_list
class CourtListView(generics.ListAPIView):
    """
    GET /api/v1/courts/

    Courts the user may book (open clubs + clubs they're a member of), each
    with its available time slots.

    Court filters (AND-combined; comma-separated values are OR-ed):
        surface_type=CLAY,HARD   is_indoor=true|false   club=<uuid>,<uuid>

    Availability window (club-local wall-clock):
        (none)                       today
        date=YYYY-MM-DD              all free slots that day
        date_from=...&date_to=...    inclusive range, at most 7 days
        time_from=HH:MM&time_to=HH:MM only slots fully inside, on each day

    Each court lists its `available_slots` and its `notices`: the type and
    notes of one-time overrides in the window (a tournament, maintenance,
    weather, …), telling members why a day differs from the usual schedule.
    When any window param is given, courts without a free slot in the window
    are omitted (they don't match the search). Without window params, every
    matching court is listed with today's slots, possibly empty.
    """

    serializer_class = CourtAvailabilitySerializer
    filterset_class = CourtFilter
    filter_backends = [DjangoFilterBackend]

    def get_queryset(self):
        return get_bookable_courts(self.request.user)

    def list(self, request, *args, **kwargs):
        query = AvailabilityQuerySerializer(data=self._window_params())
        query.is_valid(raise_exception=True)
        window = query.validated_data["window"]

        courts = list(self.filter_queryset(self.get_queryset()))
        availability = get_courts_slots(courts, window)
        if query.validated_data["is_explicit"]:
            courts = [court for court in courts if availability[court.id].available]

        page = self.paginate_queryset(courts)
        serializer = self.get_serializer(page, many=True, context={
            **self.get_serializer_context(),
            "availability": availability,
        })
        return self.get_paginated_response(serializer.data)

    def _window_params(self) -> dict:
        """Window params from the query string; blank values count as absent."""
        params = self.request.query_params
        return {key: params[key] for key in WINDOW_PARAMS if params.get(key, "").strip()}
