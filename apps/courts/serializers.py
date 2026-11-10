"""DRF serializers for the courts API."""

import zoneinfo

from django.utils import timezone
from rest_framework import serializers

from apps.bookings.availability import MAX_WINDOW_DAYS, AvailabilityWindow

from .models import Club, Court, CourtSchedule, DayTemplate, ScheduleOverride, Weekday
from .schedule import END_OF_DAY, club_today, validate_day_slots
from .services import template_locked


class EndOfDayTimeField(serializers.TimeField):
    """A time that also accepts "24:00" (midnight, end of the day), stored as time.max."""

    def to_internal_value(self, value):
        if isinstance(value, str) and value.strip() in ("24:00", "24:00:00"):
            return END_OF_DAY
        return super().to_internal_value(value)

    def to_representation(self, value):
        if value == END_OF_DAY:
            return "24:00"
        return super().to_representation(value)


WINDOW_PARAMS = ("date", "date_from", "date_to", "time_from", "time_to")


class AvailabilityQuerySerializer(serializers.Serializer):
    """
    Validates the availability window query params and resolves it to an
    AvailabilityWindow (`validated_data["window"]`):

      * none                     -> today
      * date                     -> that day
      * date_from + date_to      -> inclusive range, max MAX_WINDOW_DAYS days
      * time_from / time_to      -> optional time-of-day window applied to each day

    `validated_data["is_explicit"]` is True when any window param was sent.
    "Today" and "past" use the project timezone (settings.TIME_ZONE).
    """

    date = serializers.DateField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    time_from = serializers.TimeField(required=False)
    time_to = EndOfDayTimeField(required=False)

    def validate(self, attrs):
        single = attrs.get("date")
        date_from = attrs.get("date_from")
        date_to = attrs.get("date_to")
        time_from = attrs.get("time_from")
        time_to = attrs.get("time_to")

        if single and (date_from or date_to):
            raise serializers.ValidationError(
                "Use either `date` or `date_from`/`date_to`, not both."
            )
        if bool(date_from) != bool(date_to):
            missing = "date_to" if date_from else "date_from"
            raise serializers.ValidationError(
                {missing: "`date_from` and `date_to` must be provided together."}
            )

        today = timezone.localdate()
        if single:
            date_from = date_to = single
        elif not date_from:
            date_from = date_to = today

        if date_to < date_from:
            raise serializers.ValidationError({"date_to": "Must not be before `date_from`."})
        if date_from < today:
            field = "date" if single else "date_from"
            raise serializers.ValidationError({field: "Dates in the past are not allowed."})
        if (date_to - date_from).days + 1 > MAX_WINDOW_DAYS:
            raise serializers.ValidationError(
                {"date_to": f"The date range cannot exceed {MAX_WINDOW_DAYS} days."}
            )
        if time_from and time_to and time_from >= time_to:
            raise serializers.ValidationError({"time_to": "Must be after `time_from`."})

        return {
            "window": AvailabilityWindow(
                date_from=date_from,
                date_to=date_to,
                time_from=time_from,
                time_to=time_to,
            ),
            "is_explicit": any(param in attrs for param in WINDOW_PARAMS),
        }


class ClubSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Club
        fields = ["id", "name", "slug", "timezone", "access_type"]


class TimeSlotSerializer(serializers.Serializer):
    """Slot boundaries as ISO-8601 in the club's own timezone (offset included)."""

    start = serializers.SerializerMethodField()
    end = serializers.SerializerMethodField()

    def get_start(self, slot) -> str:
        return slot.start.isoformat()

    def get_end(self, slot) -> str:
        return slot.end.isoformat()


class NoticeSerializer(serializers.Serializer):
    """Why a day differs from the weekly schedule: a one-time override's type and notes."""

    date = serializers.DateField()
    type = serializers.CharField()
    notes = serializers.CharField()


class CourtAvailabilitySerializer(serializers.ModelSerializer):
    """
    Court + its available slots and notices. Expects `context["availability"]`:
    a mapping of court id -> CourtSlots.
    """

    club = ClubSummarySerializer(read_only=True)
    available_slots = serializers.SerializerMethodField()
    notices = serializers.SerializerMethodField()

    class Meta:
        model = Court
        fields = [
            "id",
            "name",
            "surface_type",
            "is_indoor",
            "has_lighting",
            "hourly_rate",
            "lighting_fee",
            "club",
            "available_slots",
            "notices",
        ]

    def get_available_slots(self, court) -> list[dict]:
        return TimeSlotSerializer(self.context["availability"][court.id].available, many=True).data

    def get_notices(self, court) -> list[dict]:
        return NoticeSerializer(self.context["availability"][court.id].notices, many=True).data


class ClubRegistrationSerializer(serializers.Serializer):
    """
    Input for registering a club: only its name and address. Everything else
    starts at the model defaults and is set later by editing the club; other
    fields in the request body are ignored.
    """

    name = serializers.CharField(max_length=Club._meta.get_field("name").max_length)
    address = serializers.CharField()


class ClubSerializer(serializers.ModelSerializer):
    """Full club representation (registration response and club detail)."""

    class Meta:
        model = Club
        fields = [
            "id",
            "name",
            "slug",
            "address",
            "timezone",
            "access_type",
            "cancellation_window_hours",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


AVAILABLE_TIMEZONES = frozenset(zoneinfo.available_timezones())
UNEDITABLE_CLUB_FIELDS = ("name", "address")


class ClubUpdateSerializer(serializers.ModelSerializer):
    """
    Input for editing a club: every setting except `name` and `address` —
    changing those goes through support instead, since the name anchors the
    slug and both may be referenced outside this API.
    """

    class Meta:
        model = Club
        fields = ["timezone", "access_type", "cancellation_window_hours", "is_active"]
        extra_kwargs = {field: {"required": False} for field in fields}

    def validate_timezone(self, value: str) -> str:
        if value not in AVAILABLE_TIMEZONES:
            raise serializers.ValidationError(
                f"Unknown IANA timezone name: {value!r} (e.g. 'America/Sao_Paulo')."
            )
        return value

    def validate(self, attrs):
        # Not declared as fields, so DRF would otherwise silently ignore them.
        uneditable = [field for field in UNEDITABLE_CLUB_FIELDS if field in self.initial_data]
        if uneditable:
            raise serializers.ValidationError({
                field: "Can't be changed here — contact support to update the club's "
                       "name or address." for field in uneditable
            })
        return attrs


# --- Schedules ------------------------------------------------------------------------


class WeekdayField(serializers.Field):
    """Weekday by name ("MONDAY" … "SUNDAY", case-insensitive) <-> 0 … 6."""

    default_error_messages = {
        "invalid": "Unknown weekday {value!r}; use one of " + ", ".join(Weekday.names) + ".",
    }

    def to_internal_value(self, data):
        name = str(data).strip().upper()
        if name not in Weekday.names:
            self.fail("invalid", value=data)
        return Weekday[name].value

    def to_representation(self, value):
        return Weekday(value).name


class SlotSerializer(serializers.Serializer):
    start = serializers.TimeField(format="%H:%M")
    end = EndOfDayTimeField(format="%H:%M", help_text='Use "24:00" for a slot ending at midnight.')

    def validate(self, attrs):
        for field in ("start", "end"):
            if attrs[field] != END_OF_DAY and (attrs[field].second or attrs[field].microsecond):
                raise serializers.ValidationError({field: "Use whole minutes (HH:MM)."})
        return attrs


def _slot_pairs(slots: list[dict]) -> list[tuple]:
    return [(slot["start"], slot["end"]) for slot in slots]


class DayTemplateSerializer(serializers.ModelSerializer):
    """{"id", "name", "slots": [{"start": "06:00", "end": "07:00"}, ...], "locked"}"""

    slots = serializers.SerializerMethodField()
    locked = serializers.SerializerMethodField(
        help_text="True once the template has been in effect: its slots can no longer change."
    )

    class Meta:
        model = DayTemplate
        fields = ["id", "name", "slots", "locked", "created_at", "updated_at"]

    def get_slots(self, template) -> list[dict]:
        return SlotSerializer(
            [{"start": s.start_time, "end": s.end_time} for s in template.slots.all()], many=True
        ).data

    def get_locked(self, template) -> bool:
        return template_locked(template)


class DayTemplateWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    slots = SlotSerializer(many=True, allow_empty=True)
    force = serializers.BooleanField(default=False, help_text="Cancel future bookings that no longer fit.")

    def validate_slots(self, slots):
        problems = validate_day_slots(_slot_pairs(slots))
        if problems:
            raise serializers.ValidationError(problems)
        return _slot_pairs(slots)


def _club_template(serializer, template_id):
    """Resolve a day template id within the club given in the serializer context."""
    if template_id is None:
        return None
    template = DayTemplate.objects.filter(pk=template_id, club=serializer.context["club"]).first()
    if template is None:
        raise serializers.ValidationError(f"No day template {template_id} in this club.")
    return template


class WeekdaysAssignmentSerializer(serializers.Serializer):
    """{"weekdays": ["MONDAY", ..., "FRIDAY"], "day_template": "<id>" | null (closed)}"""

    weekdays = serializers.ListField(child=WeekdayField(), allow_empty=False)
    day_template = serializers.UUIDField(allow_null=True)

    def validate_day_template(self, value):
        return _club_template(self, value)


class CourtScheduleWriteSerializer(serializers.Serializer):
    valid_from = serializers.DateField()
    valid_until = serializers.DateField(required=False, allow_null=True, default=None)
    days = WeekdaysAssignmentSerializer(many=True, allow_empty=False)
    force = serializers.BooleanField(default=False, help_text="Cancel future bookings that no longer fit.")

    def validate_days(self, assignments):
        days, repeated = {}, set()
        for assignment in assignments:
            for weekday in assignment["weekdays"]:
                if weekday in days:
                    repeated.add(Weekday(weekday).name)
                days[weekday] = assignment["day_template"]
        if repeated:
            raise serializers.ValidationError(
                f"Each weekday can appear only once; repeated: {', '.join(sorted(repeated))}."
            )
        return days


class TemplateRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = DayTemplate
        fields = ["id", "name"]


class CourtScheduleSerializer(serializers.ModelSerializer):
    """A schedule version: its period, state, and weekdays grouped by day template."""

    state = serializers.SerializerMethodField(help_text="past | current | planned")
    days = serializers.SerializerMethodField()

    class Meta:
        model = CourtSchedule
        fields = ["id", "valid_from", "valid_until", "state", "days"]

    def get_state(self, version) -> str:
        today = club_today(version.court.club)
        if version.valid_until is not None and version.valid_until < today:
            return "past"
        return "current" if version.valid_from <= today else "planned"

    def get_days(self, version) -> list[dict]:
        by_weekday = {d.weekday: d.day_template for d in version.days.all()}
        groups: dict = {}
        for weekday in Weekday.values:
            template = by_weekday.get(weekday)
            groups.setdefault(template.pk if template else None, (template, []))[1].append(Weekday(weekday).name)
        return [
            {"weekdays": weekdays, "day_template": TemplateRefSerializer(template).data if template else None}
            for template, weekdays in groups.values()
        ]


class CopyScheduleSerializer(serializers.Serializer):
    courts = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)
    valid_from = serializers.DateField()
    valid_until = serializers.DateField(required=False, allow_null=True, default=None)
    force = serializers.BooleanField(default=False)

    def validate_courts(self, ids):
        ids = list(dict.fromkeys(ids))
        courts = {c.pk: c for c in self.context["courts"].filter(pk__in=ids).select_related("club")}
        missing = [str(pk) for pk in ids if pk not in courts]
        if missing:
            raise serializers.ValidationError(f"Unknown courts (or not yours to manage): {', '.join(missing)}.")
        return [courts[pk] for pk in ids]


class ScheduleOverrideSerializer(serializers.ModelSerializer):
    day_template = TemplateRefSerializer(allow_null=True)
    slots = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleOverride
        fields = ["date", "type", "notes", "day_template", "slots", "created_at", "updated_at"]

    def get_slots(self, override) -> list[dict]:
        if override.day_template is None:
            return []
        return SlotSerializer(
            [{"start": s.start_time, "end": s.end_time} for s in override.day_template.slots.all()], many=True
        ).data


class ScheduleOverrideWriteSerializer(serializers.Serializer):
    """
    {"day_template": "<id>" | null (closed), "type": "TOURNAMENT",
     "notes": "Junior club championship until 14:00", "force": false}
    """

    day_template = serializers.UUIDField(allow_null=True, help_text="null = closed that day.")
    type = serializers.ChoiceField(choices=ScheduleOverride.Type.choices)
    notes = serializers.CharField(
        required=False, allow_blank=True, max_length=500, default="", help_text="Shown to members."
    )
    force = serializers.BooleanField(default=False)

    def validate_day_template(self, value):
        return _club_template(self, value)


class OverrideRangeSerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
