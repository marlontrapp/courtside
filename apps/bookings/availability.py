"""
Court availability: which bookable time slots are free in a date/time window.

A *slot* is one of the court's slots on a given date, as resolved from its
schedule (apps.courts.schedule: a one-time override, else the weekly
version in effect that day). A slot is *available* when it:

  * lies fully inside the requested time-of-day window (if any),
  * has not started yet, and
  * does not overlap an active reservation (PENDING_PAYMENT / CONFIRMED).

Slots a one-time override removes (a tournament, maintenance, weather, …)
don't exist that day; the override's type and notes are reported as the
court's *notices* so members can see why.

Dates and times are interpreted as wall-clock values in each club's own
timezone; returned datetimes are timezone-aware in that zone.

Cost is constant in the number of courts: a few queries to resolve the
schedules (overrides included) and one for reservations, then pure-Python
slot math.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.courts.models import Court
from apps.courts.schedule import END_OF_DAY, resolve

from .models import Reservation

MAX_WINDOW_DAYS = 7

Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class TimeSlot:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Notice:
    """Why a court's day differs from its weekly schedule (a one-time override)."""

    date: date
    type: str
    notes: str


@dataclass
class CourtSlots:
    available: list[TimeSlot]
    notices: list[Notice]


@dataclass(frozen=True)
class AvailabilityWindow:
    """Inclusive date range plus an optional time-of-day window applied to each day."""

    date_from: date
    date_to: date
    time_from: time | None = None
    time_to: time | None = None

    def __post_init__(self):
        if self.date_to < self.date_from:
            raise ValueError("date_to must not be before date_from.")
        if self.num_days > MAX_WINDOW_DAYS:
            raise ValueError(f"Window cannot exceed {MAX_WINDOW_DAYS} days.")
        if self.time_from and self.time_to and self.time_from >= self.time_to:
            raise ValueError("time_to must be after time_from.")

    @classmethod
    def for_date(cls, day: date, **times) -> AvailabilityWindow:
        return cls(date_from=day, date_to=day, **times)

    @property
    def num_days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    def days(self) -> Iterator[date]:
        for offset in range(self.num_days):
            yield self.date_from + timedelta(days=offset)


def get_courts_availability(
    courts: Iterable[Court],
    window: AvailabilityWindow,
    *,
    now: datetime | None = None,
) -> dict[UUID, list[TimeSlot]]:
    """
    Map each court id to its available slots (chronological) inside `window`.

    Courts must have `club` loaded (select_related) to avoid per-court queries.
    Every given court is present in the result, possibly with an empty list.
    """
    return {court_id: slots.available for court_id, slots in get_courts_slots(courts, window, now=now).items()}


def get_courts_slots(
    courts: Iterable[Court],
    window: AvailabilityWindow,
    *,
    now: datetime | None = None,
) -> dict[UUID, CourtSlots]:
    """Each court's available slots (chronological) and notices inside `window`."""
    courts = list(courts)
    if not courts:
        return {}
    now = now or timezone.now()

    resolved = resolve(courts, window.date_from, window.date_to)
    reserved_by_court = _reserved(courts, window)

    result: dict[UUID, CourtSlots] = {}
    for court in courts:
        tz = ZoneInfo(court.club.timezone)
        available = [
            TimeSlot(start=start, end=end)
            for day in window.days()
            for start, end in _future_slots_in_window(day, resolved.slots[court.id][day], tz, window, now)
            if not _overlaps_any(start, end, reserved_by_court[court.id])
        ]
        notices = [Notice(date=o.date, type=o.type, notes=o.notes) for o in resolved.overrides[court.id]]
        result[court.id] = CourtSlots(available=available, notices=notices)
    return result


def _reserved(courts: list[Court], window: AvailabilityWindow) -> dict[UUID, list[Interval]]:
    """Active reservations that may touch the window, per court."""
    # Pad by a day on each side so every club timezone (UTC-12..UTC+14) is covered;
    # exact overlap is checked per slot.
    lower = datetime.combine(window.date_from - timedelta(days=1), time.min, tzinfo=dt_timezone.utc)
    upper = datetime.combine(window.date_to + timedelta(days=2), time.min, tzinfo=dt_timezone.utc)

    reserved: dict[UUID, list[Interval]] = defaultdict(list)
    for court_id, start, end in Reservation.objects.filter(
        court__in=courts,
        status__in=Reservation.ACTIVE_STATUSES,
        start_datetime__lt=upper,
        end_datetime__gt=lower,
    ).values_list("court_id", "start_datetime", "end_datetime"):
        reserved[court_id].append((start, end))
    return reserved


def _future_slots_in_window(
    day: date,
    day_slots: list[tuple[time, time]],
    tz: ZoneInfo,
    window: AvailabilityWindow,
    now: datetime,
) -> Iterator[Interval]:
    """The day's scheduled slots (in order) fully inside the window that haven't started."""
    for start_time, end_time in day_slots:
        inside_window = (window.time_from is None or start_time >= window.time_from) and (
            window.time_to is None or end_time <= window.time_to or window.time_to == END_OF_DAY
        )
        if not inside_window:
            continue
        # Wall-clock slot on that date, in the club's timezone.
        start = datetime.combine(day, start_time, tzinfo=tz)
        end = datetime.combine(day, end_time, tzinfo=tz)
        if start >= now:
            yield start, end


def _overlaps_any(start: datetime, end: datetime, busy: list[Interval]) -> bool:
    return any(busy_start < end and busy_end > start for busy_start, busy_end in busy)
