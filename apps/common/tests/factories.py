"""
Plain-function model factories shared by all app test suites.

Deliberately dependency-free (no factory_boy) — each function creates a
persisted instance with sensible defaults, and every keyword argument
can be overridden by the caller:

    user = create_user(email="ana@example.com")
    court = create_court(hourly_rate=Decimal("120.00"))

If the team later adopts factory_boy, this module is the single place
to swap implementations without touching the tests themselves.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.bookings.models import Reservation
from apps.courts.models import (
    Club,
    ClubMembership,
    ClubStaffRole,
    Court,
    CourtSchedule,
    CourtScheduleDay,
    DayTemplate,
    DayTemplateSlot,
    ScheduleOverride,
)
from apps.courts.schedule import grid_slots
from apps.users.models import PlayerProfile, User

DEFAULT_PASSWORD = "test-pass-123!"
DEFAULT_TZ = "America/Sao_Paulo"

_sequence = itertools.count(1)


def _seq() -> int:
    """Monotonic counter used to keep unique fields unique across a test run."""
    return next(_sequence)


# --- users -----------------------------------------------------------------


def create_user(email: str | None = None, password: str = DEFAULT_PASSWORD, **extra) -> User:
    email = email or f"player{_seq()}@example.com"
    return User.objects.create_user(email=email, password=password, **extra)


def create_superuser(email: str | None = None, password: str = DEFAULT_PASSWORD, **extra) -> User:
    email = email or f"admin{_seq()}@example.com"
    return User.objects.create_superuser(email=email, password=password, **extra)


def create_player_profile(user: User | None = None, **extra) -> PlayerProfile:
    user = user or create_user()
    return PlayerProfile.objects.create(user=user, **extra)


# --- courts ----------------------------------------------------------------


def create_club(**extra) -> Club:
    extra.setdefault("name", f"Club {_seq()}")
    extra.setdefault("timezone", DEFAULT_TZ)
    return Club.objects.create(**extra)


def create_members_only_club(**extra) -> Club:
    extra.setdefault("access_type", Club.AccessType.MEMBERS_ONLY)
    return create_club(**extra)


def create_club_membership(
    user: User | None = None,
    club: Club | None = None,
    **extra,
) -> ClubMembership:
    """Persist a membership directly (bypassing services) for precise test setup."""
    user = user or create_user()
    club = club or create_members_only_club()
    return ClubMembership.objects.create(user=user, club=club, **extra)


def create_club_staff_role(
    user: User | None = None,
    club: Club | None = None,
    role: str = ClubStaffRole.Role.FRONT_DESK,
    **extra,
) -> ClubStaffRole:
    user = user or create_user()
    club = club or create_club()
    return ClubStaffRole.objects.create(user=user, club=club, role=role, **extra)


def create_court(club: Club | None = None, **extra) -> Court:
    club = club or create_club()
    extra.setdefault("name", f"Court {_seq()}")
    extra.setdefault("hourly_rate", Decimal("100.00"))
    extra.setdefault("lighting_fee", Decimal("20.00"))
    return Court.objects.create(club=club, **extra)


SCHEDULE_SINCE = date(2000, 1, 1)  # factory schedules cover any test date


def create_day_template(
    club: Club | None = None,
    slots: list[tuple[time, time]] | None = None,
    name: str | None = None,
) -> DayTemplate:
    club = club or create_club()
    template = DayTemplate.objects.create(club=club, name=name or f"Template {_seq()}")
    DayTemplateSlot.objects.bulk_create(
        DayTemplateSlot(template=template, start_time=start, end_time=end)
        for start, end in (slots if slots is not None else [(time(6), time(7))])
    )
    return template


def create_court_schedule(
    court: Court | None = None,
    days: dict[int, DayTemplate] | None = None,
    valid_from: date = SCHEDULE_SINCE,
    valid_until: date | None = None,
) -> CourtSchedule:
    """Persist a schedule version directly (bypassing the services' history/booking rules)."""
    court = court or create_court()
    version = CourtSchedule.objects.create(court=court, valid_from=valid_from, valid_until=valid_until)
    CourtScheduleDay.objects.bulk_create(
        CourtScheduleDay(schedule=version, weekday=weekday, day_template=template)
        for weekday, template in (days or {}).items()
    )
    return version


def create_schedule_override(
    court: Court | None = None,
    day: date | None = None,
    day_template: DayTemplate | None = None,
    **extra,
) -> ScheduleOverride:
    court = court or create_court()
    return ScheduleOverride.objects.create(
        court=court, date=day or timezone.localdate(), day_template=day_template, **extra
    )


def add_weekly_slots(
    court: Court,
    slots: list[tuple[time, time]],
    weekdays: Iterable[int] = range(7),
) -> DayTemplate:
    """
    The same slots on each of `weekdays`: a new day template assigned to
    those weekdays in the court's (single, open-ended) schedule version.
    """
    template = create_day_template(club=court.club, slots=slots)
    version = court.schedules.first() or create_court_schedule(court)
    for weekday in weekdays:
        CourtScheduleDay.objects.update_or_create(
            schedule=version, weekday=weekday, defaults={"day_template": template}
        )
    return template


def add_full_week_slots(
    court: Court,
    open_time: time = time(6, 0),
    close_time: time = time(23, 0),
    slot_duration_minutes: int = 60,
) -> DayTemplate:
    """A regular grid every day of the week — the common case in service tests."""
    return add_weekly_slots(court, grid_slots(open_time, close_time, slot_duration_minutes))


# --- bookings --------------------------------------------------------------


def future_slot(
    days_ahead: int = 1,
    hour: int = 10,
    duration_minutes: int = 60,
    tz: str = DEFAULT_TZ,
) -> tuple[datetime, datetime]:
    """
    Return an aware (start, end) pair at a fixed local wall-clock time in the
    club's timezone — keeps operating-hours assertions deterministic.
    """
    zone = ZoneInfo(tz)
    local_now = timezone.now().astimezone(zone)
    start = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(minutes=duration_minutes)


def create_reservation(
    court: Court | None = None,
    primary_player: User | None = None,
    **extra,
) -> Reservation:
    """
    Persist a Reservation directly (bypassing the booking service) so model
    and task tests can set up precise states without service-layer rules.
    """
    court = court or create_court()
    primary_player = primary_player or create_user()
    if "start_datetime" not in extra or "end_datetime" not in extra:
        start, end = future_slot(tz=court.club.timezone)
        extra.setdefault("start_datetime", start)
        extra.setdefault("end_datetime", end)
    extra.setdefault("status", Reservation.Status.CONFIRMED)
    extra.setdefault("total_price", Decimal("100.00"))
    return Reservation.objects.create(court=court, primary_player=primary_player, **extra)
