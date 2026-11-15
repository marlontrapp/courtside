"""OpenAPI documentation of the courts app's views (drf-spectacular decorators)."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import serializers

from apps.common.openapi import (
    FORCE_QUERY,
    INVALID_PAGE,
    NOT_ADMIN,
    NOT_FOUND,
    NOT_LOGGED_IN,
    SEARCH_BAD_QUERY,
    SEARCH_RANKING,
    VALIDATION_ERROR,
    DetailSerializer,
    RuleRefSerializer,
    UnlinkReportSerializer,
    search_query,
)
from apps.users.serializers import PlayerSummarySerializer

from .serializers import (
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

COURTS, CLUBS, SCHEDULES = ["Courts"], ["Clubs"], ["Schedules"]


# --- Documentation-only bodies -------------------------------------------------------


class ReservationSummarySerializer(serializers.Serializer):
    """A booking a schedule change affects."""

    id = serializers.UUIDField()
    court = serializers.UUIDField()
    court_name = serializers.CharField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    primary_player = PlayerSummarySerializer()
    status = serializers.CharField()


class ScheduleConflictSerializer(serializers.Serializer):
    detail = serializers.CharField()
    conflicting_reservations = ReservationSummarySerializer(many=True)
    hint = serializers.CharField()


class LinkedToRulesSerializer(serializers.Serializer):
    detail = serializers.CharField()
    booking_rules = RuleRefSerializer(many=True)
    hint = serializers.CharField()


class CancelledSerializer(serializers.Serializer):
    cancelled_reservations = ReservationSummarySerializer(
        many=True, help_text="Future bookings cancelled by force (no longer fitting)."
    )


class DayTemplateChangeSerializer(DayTemplateSerializer):
    cancelled_reservations = ReservationSummarySerializer(many=True, required=False)
    unlinked_booking_rules = RuleRefSerializer(many=True, required=False)
    deactivated_booking_rules = RuleRefSerializer(many=True, required=False)

    class Meta(DayTemplateSerializer.Meta):
        fields = [*DayTemplateSerializer.Meta.fields, "cancelled_reservations",
                  "unlinked_booking_rules", "deactivated_booking_rules"]


class CourtScheduleChangeSerializer(CourtScheduleSerializer):
    cancelled_reservations = ReservationSummarySerializer(many=True)

    class Meta(CourtScheduleSerializer.Meta):
        fields = [*CourtScheduleSerializer.Meta.fields, "cancelled_reservations"]


class CopiedScheduleSerializer(CourtScheduleSerializer):
    court = serializers.UUIDField()

    class Meta(CourtScheduleSerializer.Meta):
        fields = ["court", *CourtScheduleSerializer.Meta.fields]


class CopyResultSerializer(serializers.Serializer):
    schedules = CopiedScheduleSerializer(many=True)
    cancelled_reservations = ReservationSummarySerializer(many=True)


class ScheduleOverrideChangeSerializer(ScheduleOverrideSerializer):
    cancelled_reservations = ReservationSummarySerializer(many=True)

    class Meta(ScheduleOverrideSerializer.Meta):
        fields = [*ScheduleOverrideSerializer.Meta.fields, "cancelled_reservations"]


SCHEDULE_CONFLICT = OpenApiResponse(
    ScheduleConflictSerializer,
    description="Future bookings wouldn't fit anymore: they're listed. Pick a later date, or resend with force.",
)
LINKED_TO_RULES = OpenApiResponse(
    LinkedToRulesSerializer, description="Linked to booking rules (listed): unlink them, or force."
)
IN_USE = OpenApiResponse(DetailSerializer, description="Would change history, or still in use (the message says why).")


# --- Courts listing -------------------------------------------------------------------

court_list = extend_schema_view(get=extend_schema(
    tags=COURTS,
    summary="List bookable courts with their available slots",
    description="""
Active courts the user may book — courts of **open** clubs plus **members-only**
clubs where the user has an active membership — each with its free slots.

Court filters and the availability window are AND-combined; comma-separated
values are OR-ed. **Window:** no date/time params → **today**; `date` → all
free slots that day; `date_from` + `date_to` → an inclusive range of at most
7 days; `time_from` / `time_to` → only slots fully inside this time of day,
each day. Dates/times are wall-clock in each club's timezone; past dates are
rejected. When any date/time param is given, courts with **no** free slot in
the window are omitted (e.g. a court taken by a tournament at that time);
without them every matching court is listed, with today's slots.

Each court lists:
- `is_singles_only` — a singles court: no doubles, at most 2 players;
- `available_slots` — its slots that haven't started and don't overlap an
  active booking; each slot's `rules` lists the ids of the booking rules
  covering it;
- `notices` — one-time overrides in the window (`type`, `notes`): why a day
  differs from the usual schedule (e.g. no morning slots: a tournament);
- `booking_rules` — the rules its slots are subject to (`name`, `summary`,
  e.g. "Women only"). Whether the user meets them is decided when booking.
""",
    parameters=[AvailabilityQuerySerializer],
    responses={200: CourtAvailabilitySerializer(many=True), 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN},
))


# --- Managing a club's courts (club admins, site staff) --------------------------------


class CourtChangeSerializer(CourtSerializer):
    cancelled_reservations = ReservationSummarySerializer(
        many=True, help_text="Future bookings cancelled by force (deactivating the court)."
    )

    class Meta(CourtSerializer.Meta):
        fields = [f for f in CourtSerializer.Meta.fields if f != "force"] + ["cancelled_reservations"]


court_manage_list = extend_schema_view(
    get=extend_schema(
        tags=COURTS, operation_id="club_courts_list", summary="List a club's courts (club admins)",
        description="Every court of the club, inactive ones too. Club admins and site staff.",
        responses={200: CourtSerializer(many=True), 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    post=extend_schema(
        tags=COURTS, operation_id="club_courts_create", summary="Add a court (club admins)",
        description="""
Adds a court to a club you administer — also while the club awaits approval,
so it's ready when approved. Names are unique within the club. To be
bookable, a court also needs a weekly schedule (*Add a weekly schedule
version*); `is_singles_only`: no doubles, at most 2 players.
""",
        request=CourtSerializer,
        responses={201: CourtSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
        examples=[OpenApiExample("Clay court", request_only=True, value={
            "name": "Court 1", "surface_type": "CLAY", "has_lighting": True,
            "hourly_rate": "120.00", "lighting_fee": "30.00"})],
    ),
)

court_manage_detail = extend_schema_view(
    get=extend_schema(
        tags=COURTS, operation_id="club_courts_retrieve", summary="View a court (club admins)",
        responses={200: CourtSerializer, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    patch=extend_schema(
        tags=COURTS, operation_id="club_courts_update", summary="Edit a court (club admins)",
        description="""
Any subset of the settings. **Deactivating** (`"is_active": false`) a court
with future bookings is refused (`409` listing them) unless `"force": true`,
which cancels them. Other changes never affect existing bookings: their
price and lighting are kept, and a court becoming singles-only keeps its
doubles.
""",
        request=CourtSerializer(partial=True),
        responses={200: CourtChangeSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN, 404: NOT_ADMIN,
                   409: SCHEDULE_CONFLICT},
    ),
    delete=extend_schema(
        tags=COURTS, operation_id="club_courts_destroy", summary="Delete a court (club admins)",
        description="""
Only a court **without bookings** — bookings are history: deactivate it
instead (`409`). Linked to booking rules: `409` listing them, or
`?force=true` to unlink them (a rule left without a court link is
deactivated). Without force: `204`; with force: `200` and the report.
""",
        parameters=[FORCE_QUERY],
        responses={200: UnlinkReportSerializer, 204: OpenApiResponse(description="Deleted."),
                   403: NOT_LOGGED_IN, 404: NOT_ADMIN,
                   409: OpenApiResponse(LinkedToRulesSerializer, description=(
                       "Has bookings (`detail` only: deactivate it instead), or linked to booking rules."))},
    ),
)


# --- Clubs ----------------------------------------------------------------------------

club_registration = extend_schema_view(post=extend_schema(
    tags=CLUBS,
    summary="Register a club",
    description="""
Any logged-in user can register a club from just its **name** and **address**
(both required, non-blank). It starts **awaiting approval**
(`approval_status: PENDING`): hidden from players — search, courts, booking —
until site staff approve it (you're emailed the decision). You become its
**club admin**, so you can set it up meanwhile: edit its settings, day
templates, schedules and booking rules. At most 3 clubs awaiting approval per
user (`429`). Registered by site staff: approved right away.

Everything else starts at its default — timezone `America/Sao_Paulo`, access
`OPEN`, 24 h cancellation window, active — and is set later by editing the
club; other fields sent are ignored. Names needn't be unique, but slugs are:
the name (`tennis-club`); if taken, name + address
(`tennis-club-rua-augusta-100`); then name + address + a number (`…-2`, `-3`, …).
""",
    request=ClubRegistrationSerializer,
    responses={201: ClubSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN,
               429: OpenApiResponse(DetailSerializer, description="Too many of your clubs are awaiting approval.")},
    examples=[OpenApiExample("Club", request_only=True,
                             value={"name": "Ibirapuera Tennis Center", "address": "Av. Pedro Álvares Cabral, São Paulo"})],
))

club_search = extend_schema_view(get=extend_schema(
    tags=CLUBS,
    summary="Find clubs by name and address",
    description=f"""
**Active** clubs — open and members-only (`access_type` tells them apart) —
whose **name or address** contains every word of `q`. The address is free
text, so anything in it is searchable: street, number, neighbourhood, city,
state, country. Names and addresses are returned exactly as entered
(accents included).

{SEARCH_RANKING}
""",
    parameters=[search_query(
        "club's name or address",
        "`ibira` (partial word), `sao paulo` / `SÃO PAULO`, `tennis portugal` (name + address), "
        "`av paulista, 1000`",
    )],
    responses={200: ClubSearchResultSerializer(many=True), 400: SEARCH_BAD_QUERY, 403: NOT_LOGGED_IN,
               404: INVALID_PAGE},
))

club_detail = extend_schema_view(
    get=extend_schema(
        tags=CLUBS, summary="View a club",
        description=(
            "Any logged-in user may fetch an approved club. A club awaiting approval (or rejected — see "
            "`rejection_reason`) only its club admins and site staff: for others it doesn't exist (`404`)."
        ),
        responses={200: ClubSerializer, 403: NOT_LOGGED_IN, 404: NOT_FOUND},
    ),
    patch=extend_schema(
        tags=CLUBS,
        summary="Edit a club (its admins, site staff)",
        description="""
**The club's admins and site staff.** Every setting can be changed **except `name` and
`address`** — that's a support request, rejected with a `400` pointing at
support. Send any subset of the editable fields. Changing `timezone` only
affects how *new* wall-clock times are interpreted (existing bookings keep
their instant, so their local time shifts); switching to `MEMBERS_ONLY`
doesn't affect existing bookings either.
""",
        request=ClubUpdateSerializer,
        responses={200: ClubSerializer, 400: VALIDATION_ERROR,
                   403: OpenApiResponse(DetailSerializer, description="Not logged in, or not staff nor a club admin."),
                   404: NOT_FOUND},
    ),
)

STAFF_ONLY = OpenApiResponse(DetailSerializer, description="Not logged in, or not site staff.")

club_pending = extend_schema_view(get=extend_schema(
    tags=CLUBS, summary="Clubs awaiting approval (site staff)",
    description="Self-registered clubs awaiting approval, oldest first, with who registered them.",
    responses={200: ClubReviewSerializer(many=True), 403: STAFF_ONLY, 404: OpenApiResponse(DetailSerializer)},
))

club_approve = extend_schema_view(post=extend_schema(
    tags=CLUBS, summary="Approve a club (site staff)",
    description="Makes a club awaiting approval (or rejected) live; whoever registered it is emailed.",
    request=None,
    responses={200: ClubReviewSerializer, 403: STAFF_ONLY, 404: NOT_FOUND,
               409: OpenApiResponse(DetailSerializer, description="Already approved.")},
))

club_reject = extend_schema_view(post=extend_schema(
    tags=CLUBS, summary="Reject a club (site staff)",
    description=(
        "Rejects a club awaiting approval: it stays hidden, and whoever registered it is emailed the `reason`. "
        "It can still be approved later. (To hide an approved club, deactivate it.)"
    ),
    request=ClubRejectSerializer,
    responses={200: ClubReviewSerializer, 400: VALIDATION_ERROR, 403: STAFF_ONLY, 404: NOT_FOUND,
               409: OpenApiResponse(DetailSerializer, description="Not awaiting approval.")},
))


# --- Schedules ------------------------------------------------------------------------

day_template_list = extend_schema_view(
    get=extend_schema(
        tags=SCHEDULES, operation_id="day_templates_list", summary="List a club's day templates",
        responses={200: DayTemplateSerializer(many=True), 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    post=extend_schema(
        tags=SCHEDULES, operation_id="day_templates_create", summary="Create a day template",
        description='Named slot list (slot configuration), e.g. *Workdays*. Slots never overlap; `"24:00"` ends one at midnight.',
        request=DayTemplateWriteSerializer,
        responses={201: DayTemplateSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
        examples=[OpenApiExample("Workdays", request_only=True, value={
            "name": "Workdays", "slots": [{"start": "07:00", "end": "08:00"}, {"start": "18:00", "end": "24:00"}]})],
    ),
)

day_template_detail = extend_schema_view(
    get=extend_schema(
        tags=SCHEDULES, operation_id="day_templates_retrieve", summary="View a day template",
        description="Each slot has an `id` — e.g. to link a booking rule to it.",
        responses={200: DayTemplateSerializer, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    patch=extend_schema(
        tags=SCHEDULES, operation_id="day_templates_update", summary="Rename a day template or change its slots",
        description="""
Rename anytime. `slots` can change until the template is `locked` (it has
applied to a past day: `409`); unchanged slots keep their `id`. Future
bookings no longer fitting: `409`, or cancelled with `"force": true`.
Removing a slot linked to a booking rule: `409`, or `"force": true` — the
links are removed and rules left without a slot link are deactivated.
""",
        request=DayTemplateWriteSerializer(partial=True),
        responses={200: DayTemplateChangeSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN, 404: NOT_ADMIN,
                   409: OpenApiResponse(OpenApiTypes.OBJECT, description=(
                       "Locked, bookings in the way (`conflicting_reservations`), or slots linked to "
                       "booking rules (`booking_rules`)."))},
    ),
    delete=extend_schema(
        tags=SCHEDULES, operation_id="day_templates_destroy", summary="Delete a day template",
        description="""
Only when no schedule or override uses it (`409`). Linked to booking rules
(itself or its slots): `409` listing them, or `?force=true` to unlink them —
a rule left without any link of that kind is deactivated rather than
covering more. Without force: `204`; with force: `200` and the report.
""",
        parameters=[FORCE_QUERY],
        responses={200: UnlinkReportSerializer,
                   204: OpenApiResponse(description="Deleted."), 403: NOT_LOGGED_IN, 404: NOT_ADMIN,
                   409: OpenApiResponse(LinkedToRulesSerializer, description=(
                       "Used by a schedule or override (`detail` only), or linked to booking rules."))},
    ),
)

court_schedule_list = extend_schema_view(
    get=extend_schema(
        tags=SCHEDULES, operation_id="court_schedules_list", summary="List a court's weekly schedule versions",
        description="Past, current and planned versions (`state`), oldest first.",
        responses={200: CourtScheduleSerializer(many=True), 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    post=extend_schema(
        tags=SCHEDULES, operation_id="court_schedules_create", summary="Add a weekly schedule version",
        description="""
From `valid_from` (today or later). Weekdays are grouped by template;
weekdays not sent keep the template they have on `valid_from`; `null`
closes them. Without `valid_until` it lasts until the next planned version.
The version in effect is cut short (with `valid_until`, it resumes afterwards).
""",
        request=CourtScheduleWriteSerializer,
        responses={201: CourtScheduleChangeSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN,
                   404: NOT_ADMIN, 409: SCHEDULE_CONFLICT},
        examples=[OpenApiExample("Workdays and weekend", request_only=True, value={
            "valid_from": "2030-03-01",
            "days": [{"weekdays": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"],
                      "day_template": "<workdays id>"},
                     {"weekdays": ["SATURDAY", "SUNDAY"], "day_template": "<weekend id>"}],
            "force": False})],
    ),
)

court_schedule_detail = extend_schema_view(
    get=extend_schema(
        tags=SCHEDULES, operation_id="court_schedules_retrieve", summary="View a weekly schedule version",
        responses={200: CourtScheduleSerializer, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    delete=extend_schema(
        tags=SCHEDULES, operation_id="court_schedules_destroy", summary="Delete a planned schedule version",
        description="Versions starting today or later only; the version before it extends over the freed period.",
        parameters=[FORCE_QUERY],
        responses={200: CancelledSerializer, 403: NOT_LOGGED_IN, 404: NOT_ADMIN,
                   409: OpenApiResponse(OpenApiTypes.OBJECT, description="Past version, or bookings in the way.")},
    ),
)

court_schedule_copy = extend_schema_view(post=extend_schema(
    tags=SCHEDULES, operation_id="court_schedules_copy", summary="Copy a court's weekly schedule to other courts",
    description="""
Gives other courts of the **same club** the weekly schedule this court has on
`valid_from` (all 7 days), as a new version on each. All-or-nothing; one-time
overrides aren't copied.
""",
    request=CopyScheduleSerializer,
    responses={201: CopyResultSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN, 404: NOT_ADMIN,
               409: SCHEDULE_CONFLICT},
))

override_list = extend_schema_view(get=extend_schema(
    tags=SCHEDULES, operation_id="schedule_overrides_list", summary="List a court's one-time overrides",
    description="From today on by default.",
    parameters=[OverrideRangeSerializer],
    responses={200: ScheduleOverrideSerializer(many=True), 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN,
               404: NOT_ADMIN},
))

DAY_PARAM = OpenApiParameter("day", OpenApiTypes.DATE, OpenApiParameter.PATH, description="The date (YYYY-MM-DD).")

override_detail = extend_schema_view(
    get=extend_schema(
        tags=SCHEDULES, operation_id="schedule_overrides_retrieve", summary="View a one-time override",
        parameters=[DAY_PARAM],
        responses={200: ScheduleOverrideSerializer, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    put=extend_schema(
        tags=SCHEDULES, operation_id="schedule_overrides_set", summary="Set a one-time override",
        description="""
Creates or replaces the override for that date (today or later): another
day template, or `null` to close the court. The `type` and `notes` are shown
to members (courts listing `notices`, booking errors).
""",
        parameters=[DAY_PARAM],
        request=ScheduleOverrideWriteSerializer,
        responses={200: ScheduleOverrideChangeSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN,
                   404: NOT_ADMIN, 409: SCHEDULE_CONFLICT},
        examples=[OpenApiExample("Tournament", request_only=True, value={
            "day_template": "<afternoon template id>", "type": "TOURNAMENT",
            "notes": "Junior club championship until 14:00", "force": False})],
    ),
    delete=extend_schema(
        tags=SCHEDULES, operation_id="schedule_overrides_destroy", summary="Remove a one-time override",
        description="Back to the weekly schedule that day (today or later).",
        parameters=[DAY_PARAM, FORCE_QUERY],
        responses={200: CancelledSerializer, 403: NOT_LOGGED_IN, 404: NOT_ADMIN, 409: SCHEDULE_CONFLICT},
    ),
)
