"""DRF serializers for the reservations API."""

from datetime import datetime
from functools import reduce
from operator import or_
from zoneinfo import ZoneInfo

from django.db.models import Q
from rest_framework import serializers

from apps.courts.models import Court
from apps.courts.serializers import ClubSummarySerializer, EndOfDayTimeField
from apps.courts.services import get_bookable_courts
from apps.users.models import User

from .models import Reservation
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
        child=serializers.EmailField(),
        required=False,
        default=list,
        max_length=MAX_GUESTS,
        help_text="Emails of registered players joining the booking.",
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")

    def get_fields(self):
        fields = super().get_fields()
        # Only courts this user may book: a members-only court is reported
        # exactly like a non-existent id, so its existence isn't leaked.
        fields["court"].queryset = get_bookable_courts(self.context["request"].user)
        return fields

    def validate_guests(self, emails: list[str]) -> list[User]:
        unique = list(dict.fromkeys(email.strip().lower() for email in emails))
        if not unique:
            return []
        users = list(User.objects.filter(reduce(or_, (Q(email__iexact=e) for e in unique))))
        found = {user.email.lower() for user in users}
        missing = [email for email in unique if email not in found]
        if missing:
            raise serializers.ValidationError(f"No registered player with email: {', '.join(missing)}.")
        if self.context["request"].user in users:
            raise serializers.ValidationError("You can't add yourself as a guest.")
        return users

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
        fields = ["id", "name", "surface_type", "is_indoor", "club"]


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
    primary_player = serializers.SlugRelatedField(read_only=True, slug_field="email")
    guests = serializers.SlugRelatedField(many=True, read_only=True, slug_field="email")

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

    def get_state(self, reservation) -> str:
        # Use the SQL annotation when present (list), else compute (create).
        return getattr(reservation, "state", None) or reservation.state_at()

    def get_role(self, reservation) -> str:
        user = self.context["request"].user
        return "owner" if reservation.primary_player_id == user.pk else "guest"

    def get_can_cancel(self, reservation) -> bool:
        return can_cancel(reservation, self.context["request"].user)

    def get_cancellation_deadline(self, reservation) -> str:
        return self._in_club_tz(reservation, cancellation_deadline(reservation))

    def get_start(self, reservation) -> str:
        return self._in_club_tz(reservation, reservation.start_datetime)

    def get_end(self, reservation) -> str:
        return self._in_club_tz(reservation, reservation.end_datetime)

    @staticmethod
    def _in_club_tz(reservation, value: datetime) -> str:
        return value.astimezone(ZoneInfo(reservation.court.club.timezone)).isoformat()


class ReservationCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
