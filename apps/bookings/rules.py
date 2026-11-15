"""
Booking rules — conditions clubs put on bookings (models.BookingRule).

A rule is linked to where it applies — the club, courts, day templates (slot
configurations) and/or single template slots; see BookingRule — and has a
kind (what it requires). Kinds live in RULE_KINDS:

  booking_types            only some booking types (e.g. doubles only in the evening)
  player_gender            only players of some genders (women only)
  player_age               only players within an age range, on the match day
  max_bookings_per_period  at most N bookings per day / week / month
  max_active_bookings      at most N upcoming bookings at a time

Adding a kind: subclass RuleKind (key, label, description, a Params
serializer, summary() and check()) and decorate it with @register. Params are
stored as JSON, so no migration is needed.

Rules are checked when a booking is made (services.create_reservation);
changing a rule never affects existing bookings. Messages never reveal a
guest's personal data — only the booker's own.

Count limits apply to the booker and to every guest who is a member of the
club: a member's bookings count towards their limits whether they booked or
were a guest. Non-member guests don't count.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import cached_property
from typing import ClassVar
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import Q
from rest_framework import serializers

from apps.courts.models import Club, ClubMembership, Court
from apps.courts.schedule import resolve
from apps.users.models import PlayerProfile, User

from .models import BookingRule, BookingRuleCourt, BookingRuleDayTemplate, BookingRuleSlot, Reservation

logger = logging.getLogger(__name__)

# Bookings that count towards limits: everything except cancelled / expired.
COUNTED_STATUSES = (
    Reservation.Status.PENDING_PAYMENT,
    Reservation.Status.CONFIRMED,
    Reservation.Status.COMPLETED,
)

APPLIES_TO_CHOICES = [("booker", "The booker only"), ("all_players", "The booker and every named guest")]
COUNTS_CHOICES = [
    ("matching", "Only the player's bookings this rule covers (same courts, days and hours)"),
    ("club", "Every booking of the player at the club"),
]

# pg_advisory_xact_lock(namespace, key): serializes one player's bookings while
# count-based rules are checked, so two concurrent bookings can't both pass.
_PLAYER_LOCK_NAMESPACE = 0x6272  # "br"


@dataclass(frozen=True)
class BookingAttempt:
    """Everything a rule may look at about a booking being made."""

    club: Club
    court: Court
    booker: User
    guests: tuple[User, ...]
    booking_type: str
    start: datetime
    end: datetime
    now: datetime

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.club.timezone)

    @cached_property
    def day(self) -> date:
        return self.start.astimezone(self.tz).date()

    @cached_property
    def local_times(self) -> tuple[time, time]:
        return self.start.astimezone(self.tz).time(), self.end.astimezone(self.tz).time()

    @cached_property
    def covered(self) -> tuple:
        """(day template the court uses that day, ids of its slots the booking covers)."""
        start, end = self.local_times
        return resolve([self.court], self.day, self.day).covered(self.court.pk, self.day, start, end)

    @cached_property
    def members(self) -> frozenset:
        """Ids of the players (booker, guests) with an active membership of the club — one query."""
        players = [self.booker.pk, *(guest.pk for guest in self.guests)]
        return frozenset(
            ClubMembership.objects.active(at=self.now).filter(club=self.club, user_id__in=players)
            .values_list("user_id", flat=True)
        )

    @cached_property
    def counted_players(self) -> list[User]:
        """Whose limits a booking counts towards: the booker, and guests who are club members."""
        return [self.booker, *(guest for guest in self.guests if guest.pk in self.members)]

    @cached_property
    def profiles(self) -> dict:
        """Each player's profile (None without one) — one query."""
        players = [self.booker, *self.guests]
        found = {p.user_id: p for p in PlayerProfile.objects.filter(user__in=players)}
        return {player.pk: found.get(player.pk) for player in players}


@dataclass(frozen=True)
class RuleInfo:
    """A rule as shown to players (availability listing)."""

    id: object
    name: str
    kind: str
    summary: str


# --- Kinds --------------------------------------------------------------------------


class RuleKind:
    key: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[serializers.Serializer]]
    # Checks count the booker's bookings: serialize their concurrent bookings.
    locks_player: ClassVar[bool] = False

    def validate_params(self, data) -> dict:
        """Validated, normalized params (JSON-ready). Raises serializers.ValidationError."""
        if not isinstance(data, dict):
            raise serializers.ValidationError("Must be an object.")
        params = self.Params(data=data)
        params.is_valid(raise_exception=True)
        return self.normalize(dict(params.validated_data))

    def normalize(self, params: dict) -> dict:
        return params

    def summary(self, rule: BookingRule) -> str:
        """Short description for players, e.g. "Women only"."""
        raise NotImplementedError

    def check(self, rule: BookingRule, attempt: BookingAttempt) -> str | None:
        """Why `attempt` breaks the rule (a message for the booker), or None."""
        raise NotImplementedError


RULE_KINDS: dict[str, RuleKind] = {}


def register(kind_class: type[RuleKind]) -> type[RuleKind]:
    RULE_KINDS[kind_class.key] = kind_class()
    return kind_class


def _unique(values: list) -> list:
    return list(dict.fromkeys(values))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


@register
class BookingTypesKind(RuleKind):
    key = "booking_types"
    label = "Booking types"
    description = (
        "Only some booking types — e.g. doubles only in the evening slots, or lessons only on a "
        "court. (A singles court is a property of the court, not a rule: Court.is_singles_only.)"
    )

    class Params(serializers.Serializer):
        allowed = serializers.ListField(
            child=serializers.ChoiceField(choices=Reservation.BookingType.choices), allow_empty=False,
            help_text="Booking types that may be booked.",
        )

    def normalize(self, params):
        return {"allowed": _unique(params["allowed"])}

    def summary(self, rule):
        labels = [str(Reservation.BookingType(value).label) for value in rule.params["allowed"]]  # labels are lazy
        return f"{' / '.join(labels)} only"

    def check(self, rule, attempt):
        if attempt.booking_type in rule.params["allowed"]:
            return None
        booked = str(Reservation.BookingType(attempt.booking_type).label).lower()
        return f"{rule.name}: {self.summary(rule).lower()} (this is a {booked} booking)."


class _PlayerKind(RuleKind):
    """A requirement on each player's profile — the booker's, and named guests' if applies_to says so."""

    missing_hint: ClassVar[str]  # what the booker should add to their profile

    def check(self, rule, attempt):
        summary = self.summary(rule)
        booker_ok = self.meets(rule, attempt.profiles[attempt.booker.pk], attempt)
        if booker_ok is None:
            return f"{rule.name}: {summary.lower()} — {self.missing_hint}"
        if not booker_ok:
            return f"{rule.name}: {summary.lower()}."
        if rule.params.get("applies_to", "all_players") == "all_players":
            if not all(self.meets(rule, attempt.profiles[guest.pk], attempt) for guest in attempt.guests):
                # Never say which guest or why: that's their personal data.
                return f"{rule.name}: {summary.lower()} — at least one of your guests doesn't meet it."
        return None

    def meets(self, rule, profile: PlayerProfile | None, attempt) -> bool | None:
        """True / False, or None when the profile lacks the data."""
        raise NotImplementedError


@register
class PlayerGenderKind(_PlayerKind):
    key = "player_gender"
    label = "Player gender"
    description = "Only players of some genders (from their profile), e.g. a women-only slot."
    missing_hint = "add your gender to your profile if this applies to you."

    class Params(serializers.Serializer):
        allowed = serializers.ListField(
            child=serializers.ChoiceField(
                choices=[(g.value, g.label) for g in PlayerProfile.Gender if g != PlayerProfile.Gender.PREFER_NOT_TO_SAY]
            ),
            allow_empty=False, help_text="Genders that may book.",
        )
        applies_to = serializers.ChoiceField(choices=APPLIES_TO_CHOICES, default="all_players")

    def normalize(self, params):
        return {"allowed": _unique(params["allowed"]), "applies_to": params["applies_to"]}

    def summary(self, rule):
        allowed = rule.params["allowed"]
        if allowed == [PlayerProfile.Gender.FEMALE]:
            return "Women only"
        if allowed == [PlayerProfile.Gender.MALE]:
            return "Men only"
        return f"{' / '.join(str(PlayerProfile.Gender(g).label) for g in allowed)} players only"

    def meets(self, rule, profile, attempt):
        gender = profile.gender if profile else ""
        if gender in ("", PlayerProfile.Gender.PREFER_NOT_TO_SAY):
            return None
        return gender in rule.params["allowed"]


@register
class PlayerAgeKind(_PlayerKind):
    key = "player_age"
    label = "Player age"
    description = "Only players within an age range on the day of the match (from their birth date)."
    missing_hint = "add your birth date to your profile."

    class Params(serializers.Serializer):
        min_age = serializers.IntegerField(min_value=0, max_value=120, required=False, allow_null=True)
        max_age = serializers.IntegerField(min_value=0, max_value=120, required=False, allow_null=True)
        applies_to = serializers.ChoiceField(choices=APPLIES_TO_CHOICES, default="all_players")

        def validate(self, attrs):
            low, high = attrs.get("min_age"), attrs.get("max_age")
            if low is None and high is None:
                raise serializers.ValidationError("Give min_age, max_age or both.")
            if low is not None and high is not None and low > high:
                raise serializers.ValidationError("min_age can't be above max_age.")
            return attrs

    def normalize(self, params):
        return {"min_age": params.get("min_age"), "max_age": params.get("max_age"), "applies_to": params["applies_to"]}

    def summary(self, rule):
        low, high = rule.params.get("min_age"), rule.params.get("max_age")
        if low is not None and high is not None:
            return f"Ages {low}–{high}"
        return f"Ages {low}+" if low is not None else f"Ages up to {high}"

    def meets(self, rule, profile, attempt):
        age = profile.age(on=attempt.day) if profile else None
        if age is None:
            return None
        low, high = rule.params.get("min_age"), rule.params.get("max_age")
        return (low is None or age >= low) and (high is None or age <= high)


class _CountKind(RuleKind):
    """
    Limits on how many bookings a player has at the club. Checked for the
    booker and for each guest who is a club member; a member's bookings as a
    guest count too (see player_bookings).
    """

    locks_player = True

    def check(self, rule, attempt):
        for player in attempt.counted_players:
            count = self.count(rule, attempt, player)
            if count < rule.params["max"]:
                continue
            if player.pk == attempt.booker.pk:
                return self.booker_message(rule, count)
            # Never say which guest, nor their count: that's their data.
            return f"{rule.name}: {self.summary(rule).lower()} — at least one of your guests has already reached it."
        return None

    def count(self, rule, attempt, player) -> int:
        raise NotImplementedError

    def booker_message(self, rule, count: int) -> str:
        raise NotImplementedError

    def _counted(self, rule, attempt, bookings) -> int:
        """How many of `bookings` ((court_id, start, end)) count for this rule."""
        if rule.params.get("counts", "matching") == "club" or not bookings:
            return len(bookings)
        tz = attempt.tz
        local = [(court_id, start.astimezone(tz), end.astimezone(tz)) for court_id, start, end in bookings]
        days = [start.date() for _, start, _ in local]
        resolved = resolve(Court.objects.filter(pk__in={court_id for court_id, _, _ in local}), min(days), max(days))
        return sum(
            1
            for court_id, start, end in local
            if rule.covers(court_id, *resolved.covered(court_id, start.date(), start.time(), end.time()))
        )

    def _where(self, rule) -> str:
        return " at the club" if rule.params.get("counts", "matching") == "club" else ""


def player_bookings(attempt: BookingAttempt, player: User, statuses):
    """
    `player`'s bookings at the club with `statuses`: as the booker, plus as a
    guest when they're a member of the club (then they count like their own).
    """
    involved = Q(primary_player=player)
    if player.pk in attempt.members:
        involved |= Q(guests=player)
    return Reservation.objects.filter(involved, court__club=attempt.club, status__in=statuses).distinct()


PERIOD_CHOICES = [("day", "Day"), ("week", "Week (Monday to Sunday)"), ("month", "Month")]


def period_bounds(day: date, period: str) -> tuple[date, date]:
    """First and last day of the calendar day / week / month containing `day`."""
    if period == "day":
        return day, day
    if period == "week":
        first = day - timedelta(days=day.weekday())
        return first, first + timedelta(days=6)
    return day.replace(day=1), day.replace(day=monthrange(day.year, day.month)[1])


@register
class MaxBookingsPerPeriodKind(_CountKind):
    key = "max_bookings_per_period"
    label = "Bookings per period"
    description = (
        "At most N bookings per calendar day, week or month per player — club-wide, or only for "
        "some courts / slots, e.g. prime time, so the same players can't always take them. "
        "Members' bookings as guests count too."
    )

    class Params(serializers.Serializer):
        max = serializers.IntegerField(min_value=1, help_text="Bookings allowed per period.")
        period = serializers.ChoiceField(choices=PERIOD_CHOICES)
        counts = serializers.ChoiceField(choices=COUNTS_CHOICES, default="matching")

    def summary(self, rule):
        return f"Max {_plural(rule.params['max'], 'booking')} per {rule.params['period']}{self._where(rule)}"

    def count(self, rule, attempt, player):
        first, last = period_bounds(attempt.day, rule.params["period"])
        bookings = list(
            player_bookings(attempt, player, COUNTED_STATUSES).filter(
                start_datetime__gte=datetime.combine(first, time.min, tzinfo=attempt.tz),
                start_datetime__lt=datetime.combine(last + timedelta(days=1), time.min, tzinfo=attempt.tz),
            ).values_list("court_id", "start_datetime", "end_datetime")
        )
        return self._counted(rule, attempt, bookings)

    def booker_message(self, rule, count):
        return (
            f"{rule.name}: {self.summary(rule).lower()} — you already have "
            f"{_plural(count, 'booking')} that {rule.params['period']}."
        )


@register
class MaxActiveBookingsKind(_CountKind):
    key = "max_active_bookings"
    label = "Upcoming bookings"
    description = (
        "At most N upcoming (not yet finished) bookings at a time per player — e.g. one active "
        "booking. Members' bookings as guests count too."
    )

    class Params(serializers.Serializer):
        max = serializers.IntegerField(min_value=1, help_text="Upcoming bookings allowed at a time.")
        counts = serializers.ChoiceField(choices=COUNTS_CHOICES, default="matching")

    def summary(self, rule):
        return f"Max {_plural(rule.params['max'], 'upcoming booking')}{self._where(rule)}"

    def count(self, rule, attempt, player):
        bookings = list(
            player_bookings(attempt, player, Reservation.ACTIVE_STATUSES)
            .filter(end_datetime__gt=attempt.now)
            .values_list("court_id", "start_datetime", "end_datetime")
        )
        return self._counted(rule, attempt, bookings)

    def booker_message(self, rule, count):
        return f"{rule.name}: {self.summary(rule).lower()} — you already have {count}."


# --- Evaluation ---------------------------------------------------------------------


def rule_summary(rule: BookingRule) -> str:
    kind = RULE_KINDS.get(rule.kind)
    return kind.summary(rule) if kind else rule.kind


def covering_rules(rules, court_id, day: date, template_id, slot_ids) -> list[BookingRule]:
    """
    Of `rules` (one club's, links prefetched), those in force on `day` whose
    links cover court `court_id` using template `template_id` and covering
    the template slots `slot_ids`.
    """
    return [rule for rule in rules if rule.applies_on(day) and rule.covers(court_id, template_id, slot_ids)]


def active_rules(clubs) -> dict[object, list[BookingRule]]:
    """Each club's active rules, links prefetched — four queries."""
    by_club: dict[object, list[BookingRule]] = {getattr(club, "pk", club): [] for club in clubs}
    for rule in BookingRule.objects.filter(club__in=list(by_club), is_active=True).prefetch_related(
        "courts", "day_templates", "slots"
    ):
        by_club[rule.club_id].append(rule)
    return by_club


def booking_rule_violations(attempt: BookingAttempt) -> list[str]:
    """
    Messages for every rule `attempt` breaks (empty = allowed). Call inside
    the booking's transaction: count-based rules lock the players they count
    (booker, member guests) until it ends, so concurrent bookings involving
    them are checked one at a time.
    """
    template_id, slot_ids = attempt.covered
    rules = covering_rules(active_rules([attempt.club])[attempt.club.pk], attempt.court.pk, attempt.day, template_id, slot_ids)
    known = []
    for rule in rules:
        if rule.kind in RULE_KINDS:
            known.append(rule)
        else:  # validated on save, so only if a kind was removed from the code
            logger.error("Booking rule %s has an unknown kind %r; ignored.", rule.pk, rule.kind)
    if any(RULE_KINDS[rule.kind].locks_player for rule in known):
        lock_players(attempt.counted_players)
    return [message for rule in known if (message := RULE_KINDS[rule.kind].check(rule, attempt))]


def _lock_key(user: User) -> int:
    key = user.pk.int % 2**32
    return key - 2**32 if key >= 2**31 else key  # int4


def lock_players(users) -> None:
    """
    Transaction-scoped advisory locks on players (released at commit/rollback),
    taken in a fixed order so two bookings sharing players can't deadlock.
    """
    with connection.cursor() as cursor:
        for key in sorted({_lock_key(user) for user in users}):
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_PLAYER_LOCK_NAMESPACE, key])


# --- Links to deleted courts / templates / slots ----------------------------------------


@dataclass
class UnlinkReport:
    """What unlink_from_rules() did: rules that lost links, and those deactivated instead of widening."""

    unlinked: list[BookingRule]
    deactivated: list[BookingRule]


def linked_rules(*, courts=(), day_templates=(), slots=()):
    """Rules linked to any of these courts / day templates / slots (each rule once, by name)."""
    return BookingRule.objects.filter(
        Q(pk__in=BookingRuleCourt.objects.filter(court__in=list(courts)).values("rule_id"))
        | Q(pk__in=BookingRuleDayTemplate.objects.filter(day_template__in=list(day_templates)).values("rule_id"))
        | Q(pk__in=BookingRuleSlot.objects.filter(slot__in=list(slots)).values("rule_id"))
    ).order_by("name", "pk")


def unlink_from_rules(*, courts=(), day_templates=(), slots=()) -> UnlinkReport:
    """
    Remove rules' links to these courts / day templates / slots (before they're
    deleted). A rule left with no link of a kind it had — e.g. its only court
    — would silently cover more (every court), so it's deactivated instead;
    an admin can relink and reactivate it. Rules keeping other links of that
    kind just lose the link (they cover less).
    """
    courts, day_templates, slots = list(courts), list(day_templates), list(slots)
    links = [
        (BookingRuleCourt, "court", courts),
        (BookingRuleDayTemplate, "day_template", day_templates),
        (BookingRuleSlot, "slot", slots),
    ]
    rules = list(linked_rules(courts=courts, day_templates=day_templates, slots=slots).select_for_update())
    deactivated = []
    for rule in rules:
        for model, field, removed in links:
            remaining = model.objects.filter(rule=rule).exclude(**{f"{field}__in": removed})
            if model.objects.filter(rule=rule, **{f"{field}__in": removed}).exists() and not remaining.exists():
                deactivated.append(rule)
                break
    for model, field, removed in links:
        model.objects.filter(**{f"{field}__in": removed}).delete()
    if deactivated:
        BookingRule.objects.filter(pk__in=[rule.pk for rule in deactivated]).update(is_active=False)
        for rule in deactivated:
            rule.is_active = False
        logger.info("Deactivated booking rules left without links: %s", [str(rule.pk) for rule in deactivated])
    return UnlinkReport(unlinked=rules, deactivated=deactivated)


# --- Describing kinds (for admin clients) -------------------------------------------------


def describe_kinds() -> list[dict]:
    """Every kind with its params: type, required, default, choices, limits, help."""
    return [
        {
            "kind": kind.key,
            "label": kind.label,
            "description": kind.description,
            "params": {name: _describe_field(f) for name, f in kind.Params().fields.items()},
        }
        for kind in RULE_KINDS.values()
    ]


def _describe_field(f: serializers.Field) -> dict:
    info: dict = {"required": f.required}
    if isinstance(f, serializers.ListField):
        info["type"] = "list"
        if isinstance(f.child, serializers.ChoiceField):
            info["choices"] = list(f.child.choices)
    elif isinstance(f, serializers.ChoiceField):
        info["type"] = "choice"
        info["choices"] = list(f.choices)
    elif isinstance(f, serializers.IntegerField):
        info["type"] = "integer"
        info.update({k: v for k, v in (("min", f.min_value), ("max", f.max_value)) if v is not None})
    else:
        info["type"] = type(f).__name__
    if f.default is not serializers.empty:
        info["default"] = f.default
    if f.help_text:
        info["help"] = str(f.help_text)
    return info
