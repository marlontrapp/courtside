"""DRF serializers for the reservations API."""

from datetime import datetime
from zoneinfo import ZoneInfo

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.courts.models import Court
from apps.courts.models import DayTemplate, DayTemplateSlot
from apps.courts.serializers import ClubSummarySerializer, EndOfDayTimeField
from apps.courts.services import get_bookable_courts
from apps.users.models import User
from apps.users.references import parse_player_references
from apps.users.serializers import PlayerSummarySerializer, sorted_by_name

from .models import BookingRule, Reservation
from .rules import RULE_KINDS, rule_summary
from .services import can_cancel, cancellation_deadline

# Players book matches and lessons; TOURNAMENT/BLOCKED are staff-only types.
PLAYER_BOOKING_TYPES = [
    Reservation.BookingType.SINGLES,
    Reservation.BookingType.DOUBLES,
    Reservation.BookingType.LESSON,
]
MAX_GUESTS = 3  # doubles: the booker + 3


class ReservationCreateSerializer(serializers.Serializer):
    """
    Input for booking a court. Date and times are wall-clock values in the
    court's club timezone (same convention as the courts listing filters).
    """

    court = serializers.PrimaryKeyRelatedField(queryset=Court.objects.none())
    date = serializers.DateField()
    start_time = serializers.TimeField()
    end_time = EndOfDayTimeField(help_text='Use "24:00" for a booking ending at midnight.')
    booking_type = serializers.ChoiceField(
        choices=[(t.value, t.label) for t in PLAYER_BOOKING_TYPES],
        default=Reservation.BookingType.SINGLES,
    )
    includes_lighting = serializers.BooleanField(default=False)
    guests = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        max_length=MAX_GUESTS,
        help_text=(
            "Registered players joining the booking, each given by email or by player id "
            "(the user id, as returned by the player search) — both can be mixed."
        ),
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")

    def get_fields(self):
        fields = super().get_fields()
        # Only courts this user may book: a members-only court is reported
        # exactly like a non-existent id, so its existence isn't leaked.
        fields["court"].queryset = get_bookable_courts(self.context["request"].user)
        return fields

    def validate_guests(self, entries: list[str]) -> list[User]:
        references = parse_player_references(entries)
        if references.invalid:
            raise serializers.ValidationError(f"Not a player id or email: {', '.join(references.invalid)}.")
        if not references:
            return []

        users = list(User.objects.filter(references.q()))
        found_emails = {user.email.lower() for user in users}
        found_ids = {user.pk for user in users}
        errors = []
        # Only echoes back what the requester sent: no email is revealed.
        if missing := [email for email in references.emails if email not in found_emails]:
            errors.append(f"No registered player with email: {', '.join(missing)}.")
        if missing := [str(pk) for pk in references.ids if pk not in found_ids]:
            errors.append(f"No registered player with id: {', '.join(missing)}.")
        if errors:
            raise serializers.ValidationError(errors)
        if self.context["request"].user in users:
            raise serializers.ValidationError("You can't add yourself as a guest.")
        return users  # one per player, also when given both by email and by id

    def validate(self, attrs):
        if attrs["end_time"] <= attrs["start_time"]:
            raise serializers.ValidationError({"end_time": "Must be after `start_time`."})
        zone = ZoneInfo(attrs["court"].club.timezone)
        attrs["start_datetime"] = datetime.combine(attrs["date"], attrs["start_time"], tzinfo=zone)
        attrs["end_datetime"] = datetime.combine(attrs["date"], attrs["end_time"], tzinfo=zone)
        return attrs


class ReservedCourtSerializer(serializers.ModelSerializer):
    club = ClubSummarySerializer(read_only=True)

    class Meta:
        model = Court
        fields = ["id", "name", "surface_type", "is_indoor", "is_singles_only", "club"]


class ReservationSerializer(serializers.ModelSerializer):
    """
    Output representation; times are ISO-8601 in the club's timezone.

    `status` is the stored status; `state` is the lifecycle bucket derived from
    status + time (what the `state` filter uses). `role` is relative to the
    requesting user: "owner" (booked it) or "guest". `can_cancel` says whether
    the requesting user may cancel it right now; `cancellation_deadline` is the
    club policy's cutoff for confirmed bookings.
    """

    court = ReservedCourtSerializer(read_only=True)
    start = serializers.SerializerMethodField()
    end = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()
    cancellation_deadline = serializers.SerializerMethodField()
    # Players as {id, first_name, last_name}: emails are never returned.
    primary_player = PlayerSummarySerializer(read_only=True)
    guests = serializers.SerializerMethodField()

    class Meta:
        model = Reservation
        fields = [
            "id",
            "court",
            "start",
            "end",
            "booking_type",
            "status",
            "state",
            "role",
            "primary_player",
            "includes_lighting",
            "total_price",
            "guests",
            "notes",
            "can_cancel",
            "cancellation_deadline",
            "cancelled_at",
            "cancellation_reason",
            "created_at",
        ]

    @extend_schema_field(PlayerSummarySerializer(many=True))
    def get_guests(self, reservation) -> list[dict]:
        return PlayerSummarySerializer(sorted_by_name(reservation.guests.all()), many=True).data

    @extend_schema_field({"type": "string", "enum": list(Reservation.State.values)})
    def get_state(self, reservation) -> str:
        # Use the SQL annotation when present (list), else compute (create).
        return getattr(reservation, "state", None) or reservation.state_at()

    @extend_schema_field({"type": "string", "enum": ["owner", "guest"]})
    def get_role(self, reservation) -> str:
        user = self.context["request"].user
        return "owner" if reservation.primary_player_id == user.pk else "guest"

    def get_can_cancel(self, reservation) -> bool:
        return can_cancel(reservation, self.context["request"].user)

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_cancellation_deadline(self, reservation) -> str:
        return self._in_club_tz(reservation, cancellation_deadline(reservation))

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_start(self, reservation) -> str:
        return self._in_club_tz(reservation, reservation.start_datetime)

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_end(self, reservation) -> str:
        return self._in_club_tz(reservation, reservation.end_datetime)

    @staticmethod
    def _in_club_tz(reservation, value: datetime) -> str:
        return value.astimezone(ZoneInfo(reservation.court.club.timezone)).isoformat()


class ReservationCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class BookingRuleSerializer(serializers.ModelSerializer):
    """
    A club's booking rule, for club admins (read and write). Where it applies:
    linked `courts`, `day_templates` (slot configurations) and `slots` (single
    slots of day templates) — ids of this club's; nothing linked = the whole
    club; each kind of link narrows it. `valid_from` / `valid_until`: when
    it's in force. `kind` + `params`: see GET /api/v1/booking-rule-kinds/.
    Expects `context["club"]`.
    """

    courts = serializers.PrimaryKeyRelatedField(many=True, queryset=Court.objects.none(), required=False)
    day_templates = serializers.PrimaryKeyRelatedField(many=True, queryset=DayTemplate.objects.none(), required=False)
    slots = serializers.PrimaryKeyRelatedField(many=True, queryset=DayTemplateSlot.objects.none(), required=False)
    params = serializers.JSONField(required=False)
    summary = serializers.SerializerMethodField()

    class Meta:
        model = BookingRule
        fields = [
            "id", "name", "kind", "params", "summary", "courts", "day_templates", "slots",
            "valid_from", "valid_until", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "summary", "created_at", "updated_at"]

    def get_fields(self):
        fields = super().get_fields()
        club = self.context.get("club")  # other clubs' courts / templates / slots: invalid
        if club is None:  # no club: OpenAPI schema generation
            return fields
        fields["courts"].child_relation.queryset = club.courts.all()
        fields["day_templates"].child_relation.queryset = club.day_templates.all()
        fields["slots"].child_relation.queryset = DayTemplateSlot.objects.filter(template__club=club)
        return fields

    def get_summary(self, rule) -> str:
        return rule_summary(rule)

    def validate_kind(self, kind: str) -> str:
        if kind not in RULE_KINDS:
            raise serializers.ValidationError(f"Unknown kind; use one of: {', '.join(RULE_KINDS)}.")
        return kind

    def validate(self, attrs):
        def effective(name, default=None):
            return attrs[name] if name in attrs else getattr(self.instance, name, default)

        kind = effective("kind")
        if kind is None:
            raise serializers.ValidationError({"kind": "This field is required."})
        if "kind" in attrs or "params" in attrs:  # a new kind must get valid params for it
            attrs["params"] = self._params(kind, effective("params", {}))

        templates = attrs["day_templates"] if "day_templates" in attrs else list(self.instance.day_templates.all()) if self.instance else []
        slots = attrs["slots"] if "slots" in attrs else list(self.instance.slots.all()) if self.instance else []
        if templates and slots:
            template_ids = {template.pk for template in templates}
            if any(slot.template_id not in template_ids for slot in slots):
                raise serializers.ValidationError(
                    {"slots": "With day_templates linked too, every slot must belong to one of them (else the rule never applies)."}
                )
        valid_from, valid_until = effective("valid_from"), effective("valid_until")
        if valid_from and valid_until and valid_until < valid_from:
            raise serializers.ValidationError({"valid_until": "Can't be before valid_from."})
        return attrs

    @staticmethod
    def _params(kind: str, params) -> dict:
        try:
            return RULE_KINDS[kind].validate_params(params)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"params": exc.detail}) from exc

    def create(self, validated_data):
        return super().create({**validated_data, "club": self.context["club"]})
