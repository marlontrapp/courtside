"""OpenAPI documentation of the bookings app's views (drf-spectacular decorators)."""

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers

from apps.common.openapi import (
    NOT_ADMIN,
    NOT_LOGGED_IN,
    VALIDATION_ERROR,
    DetailSerializer,
    NonFieldErrorsSerializer,
)

from .serializers import (
    BookingRuleSerializer,
    ReservationCancelSerializer,
    ReservationCreateSerializer,
    ReservationSerializer,
)

RESERVATIONS, RULES = ["Reservations"], ["Booking rules"]

reservation_list = extend_schema_view(
    get=extend_schema(
        tags=RESERVATIONS,
        summary="My bookings",
        description="""
The logged-in user's bookings — the ones they booked **and** the ones they're
a guest in (`role`: `owner` | `guest`). Filters are AND-combined;
comma-separated values within one are OR-ed; dates/times are wall-clock in
each club's timezone. `player` takes player ids and/or emails.

`state` is derived from the stored `status` and the clock, because played
bookings are never flipped to `COMPLETED` automatically: `upcoming`
(confirmed, not finished — includes a match in progress), `pending_payment`
(held, awaiting payment), `completed` (marked completed, or confirmed and
over), `cancelled`, `expired` (payment hold expired, or its slot passed).

Each booking includes `can_cancel` (for the requesting user) and
`cancellation_deadline`, so clients can show the policy before trying.
""",
        responses={200: ReservationSerializer(many=True), 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN},
    ),
    post=extend_schema(
        tags=RESERVATIONS,
        summary="Book a court",
        description="""
Books a court for the logged-in user (the booker). `date`, `start_time` and
`end_time` are wall-clock in the **club's timezone**; the time must be free
and match the court's slots — exactly what the courts listing returns as
`available_slots`. Several consecutive slots can be booked at once (e.g.
18:00–20:00): it starts when a slot starts, ends when a slot ends, and can't
span a gap.

`guests` (optional, max 3) takes each player **by email or by player id**,
mixed; the same player given twice is one guest. On a **singles court**
(`is_singles_only`): no doubles, at most 2 players (you and 1 guest).

The booking must meet the club's **booking rules** (every broken rule is
listed in the `400`). Payment isn't implemented yet, so bookings are
`CONFIRMED` immediately.
""",
        request=ReservationCreateSerializer,
        responses={
            201: ReservationSerializer,
            400: OpenApiResponse(NonFieldErrorsSerializer, description=(
                "Invalid input (per field, e.g. `guests`: `Not a player id or email: '…'`, `No registered "
                "player with email/id: …`, yourself), or not bookable (`non_field_errors`): past, not "
                "matching the court's slots — when a one-time override changed that day, the message says "
                "why (e.g. \"… — Tournament: Junior club championship until 14:00.\") —, lighting "
                "unavailable, a singles court with a doubles booking or more than 2 players, or broken "
                "booking rules. A court you can't book (e.g. members-only) is reported as not existing.")),
            403: NOT_LOGGED_IN,
            409: OpenApiResponse(NonFieldErrorsSerializer, description=(
                "The slot is already taken (also when two people book it at the same moment).")),
        },
        examples=[OpenApiExample("Doubles with guests", request_only=True, value={
            "court": "<court id>", "date": "2030-01-08", "start_time": "18:00", "end_time": "19:00",
            "booking_type": "DOUBLES", "includes_lighting": True,
            "guests": ["friend@example.com", "<player id>"], "notes": "Bring balls"})],
    ),
)

reservation_cancel = extend_schema_view(post=extend_schema(
    tags=RESERVATIONS,
    summary="Cancel a booking",
    description="""
Keeps the booking (status `CANCELLED`, with `cancelled_at` and
`cancellation_reason`) and frees the slot; the booker and guests are
notified. Only the booker can cancel — not guests. Policy, per club
(`cancellation_window_hours`, default 24): a confirmed booking until
`start − window` (0 = until the start); an unpaid hold any time before the
start; started, finished, expired or completed bookings never. Repeating the
call on a booking you already cancelled returns `200` (safe to retry).
""",
    request=ReservationCancelSerializer,
    responses={
        200: ReservationSerializer,
        403: OpenApiResponse(DetailSerializer, description="Not logged in, or you're a guest in this booking."),
        404: OpenApiResponse(DetailSerializer, description="No such booking, or you're not in it."),
        409: OpenApiResponse(DetailSerializer, description=(
            "Can't be cancelled — the message says why (e.g. the deadline passed, with the club's policy).")),
    },
    examples=[OpenApiExample("Reason", request_only=True, value={"reason": "Rain forecast"})],
))


RULE_DESCRIPTION = """
Where: linked `courts`, `day_templates` (slot configurations) and `slots`
(single slots — ids in the day template endpoints) of this club; nothing
linked = the whole club; each kind of link narrows it (court **and**
template **and** slot). When: `valid_from` / `valid_until` (optional). What:
`kind` + `params` (see *List booking rule kinds*). Rules apply to bookings
made after they're saved — never to existing ones.
"""

booking_rule_list = extend_schema_view(
    get=extend_schema(
        tags=RULES, operation_id="booking_rules_list", summary="List a club's booking rules",
        description="Club admins and site staff only.",
        responses={200: BookingRuleSerializer(many=True), 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    post=extend_schema(
        tags=RULES, operation_id="booking_rules_create", summary="Add a booking rule",
        description=RULE_DESCRIPTION,
        request=BookingRuleSerializer,
        responses={201: BookingRuleSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
        examples=[
            OpenApiExample("Women's evening", request_only=True, value={
                "name": "Women's evening", "kind": "player_gender", "params": {"allowed": ["FEMALE"]},
                "courts": ["<court 1 id>", "<court 2 id>"], "slots": ["<19:00 slot id>", "<20:00 slot id>"]}),
            OpenApiExample("Prime time: 2 a week", request_only=True, value={
                "name": "Prime time", "kind": "max_bookings_per_period",
                "params": {"max": 2, "period": "week"}, "slots": ["<18:00 slot id>", "<19:00 slot id>"]}),
            OpenApiExample("One upcoming booking", request_only=True, value={
                "name": "One at a time", "kind": "max_active_bookings", "params": {"max": 1}}),
        ],
    ),
)

booking_rule_detail = extend_schema_view(
    get=extend_schema(
        tags=RULES, operation_id="booking_rules_retrieve", summary="View a booking rule",
        responses={200: BookingRuleSerializer, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    patch=extend_schema(
        tags=RULES, operation_id="booking_rules_update", summary="Edit a booking rule",
        description=(
            "Any subset of the fields; a new `kind` needs `params` valid for it. "
            '`"is_active": false` pauses a rule. Existing bookings are never affected.'
        ),
        request=BookingRuleSerializer(partial=True),
        responses={200: BookingRuleSerializer, 400: VALIDATION_ERROR, 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
    delete=extend_schema(
        tags=RULES, operation_id="booking_rules_destroy", summary="Delete a booking rule",
        description="Existing bookings are never affected.",
        responses={204: OpenApiResponse(description="Deleted."), 403: NOT_LOGGED_IN, 404: NOT_ADMIN},
    ),
)

booking_rule_kinds = extend_schema_view(get=extend_schema(
    tags=RULES,
    summary="List booking rule kinds",
    description=(
        "Every rule kind and its `params` (type, required, default, choices, limits, help) — for building "
        "rule editors. Any logged-in user."
    ),
    responses={
        200: inline_serializer("BookingRuleKind", many=True, fields={
            "kind": serializers.CharField(),
            "label": serializers.CharField(),
            "description": serializers.CharField(),
            "params": serializers.DictField(
                child=serializers.DictField(), help_text="Param name → its type, required, default, choices, …"
            ),
        }),
        403: NOT_LOGGED_IN,
    },
))
