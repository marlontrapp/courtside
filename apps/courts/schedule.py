"""
Reading court schedules: which slots a court has on a given date.

Resolution for (court, date):
  1. a ScheduleOverride for that date, if any — its day template, or closed;
  2. otherwise the CourtSchedule version covering the date — the day template
     of that weekday, or closed if the weekday has none;
  3. no version covering the date — closed.

Slots are (start_time, end_time) wall-clock pairs in the club's timezone. A
slot ending at midnight has end_time == END_OF_DAY (time.max); "24:00" is how
the API spells it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from .models import Club, Court, CourtSchedule, DayTemplateSlot, ScheduleOverride

Slot = tuple[time, time]

END_OF_DAY = time.max  # "24:00": the slot runs until the end of the day


def club_today(club: Club) -> date:
    """Today's date in the club's timezone (schedules are per club-local day)."""
    return timezone.now().astimezone(ZoneInfo(club.timezone)).date()


def format_time(value: time) -> str:
    return "24:00" if value == END_OF_DAY else f"{value:%H:%M}"


def format_slot(slot: Slot) -> str:
    return f"{format_time(slot[0])}–{format_time(slot[1])}"


# --- Slot lists --------------------------------------------------------------------


def validate_day_slots(slots: Iterable[Slot]) -> list[str]:
    """Problems with one day's slots: end not after start, overlaps (in start order)."""
    errors = []
    ordered = sorted(slots)
    for start, end in ordered:
        if end <= start:
            errors.append(
                f"{format_slot((start, end))}: the end must be after the start "
                "(use 24:00 for a slot ending at midnight)."
            )
    valid = [(start, end) for start, end in ordered if end > start]
    for first, second in zip(valid, valid[1:]):
        if second[0] < first[1]:
            errors.append(f"{format_slot(first)} overlaps {format_slot(second)}.")
    return errors


def grid_slots(open_time: time, close_time: time, minutes: int) -> list[Slot]:
    """
    Equal-length back-to-back slots from open_time, the last one ending by
    close_time (END_OF_DAY = midnight).
    """
    if minutes <= 0:
        return []
    day = datetime(2000, 1, 1)
    cursor = datetime.combine(day, open_time)
    close = day + timedelta(days=1) if close_time == END_OF_DAY else datetime.combine(day, close_time)
    step = timedelta(minutes=minutes)
    slots = []
    while cursor + step <= close:
        end = cursor + step
        slots.append((cursor.time(), END_OF_DAY if end == day + timedelta(days=1) else end.time()))
        cursor = end
    return slots


def booking_error(day_slots: list[Slot], start: time, end: time) -> str | None:
    """
    Why [start, end) doesn't fit `day_slots` (ordered), or None if it does:
    a booking covers one or more consecutive slots — it starts when a slot
    starts, ends when a slot ends, and doesn't span a gap.
    """
    if not day_slots:
        return "Court has no time slots on this day."
    index = next((i for i, (slot_start, _) in enumerate(day_slots) if slot_start == start), None)
    if index is None:
        return f"Booking must start when one of the court's slots starts ({describe_day(day_slots)})."
    while day_slots[index][1] < end:
        if index + 1 == len(day_slots) or day_slots[index + 1][0] != day_slots[index][1]:
            return (
                "Booking must end when one of the court's slots ends, covering consecutive "
                f"slots without a gap ({describe_day(day_slots)})."
            )
        index += 1
    if day_slots[index][1] != end:
        return f"Booking must end when one of the court's slots ends ({describe_day(day_slots)})."
    return None


def describe_day(day_slots: list[Slot], limit: int = 6) -> str:
    shown = ", ".join(format_slot(slot) for slot in day_slots[:limit])
    return f"that day: {shown}{', …' if len(day_slots) > limit else ''}"


# --- Resolution --------------------------------------------------------------------


@dataclass
class Resolved:
    """resolve() result: slots per court and date, and the overrides that applied."""

    slots: dict[UUID, dict[date, list[Slot]]]
    overrides: dict[UUID, list[ScheduleOverride]]  # per court, by date


def resolve_slots(
    courts: Iterable[Court], date_from: date, date_to: date
) -> dict[UUID, dict[date, list[Slot]]]:
    """Each court's slots for every date in [date_from, date_to] (closed days map to [])."""
    return resolve(courts, date_from, date_to).slots


def resolve(courts: Iterable[Court], date_from: date, date_to: date) -> Resolved:
    """
    Each court's slots for every date in [date_from, date_to], plus the
    overrides in that range. Four queries regardless of the number of courts
    or days.
    """
    courts = list(courts)
    court_ids = [court.pk for court in courts]
    versions = list(
        CourtSchedule.objects.filter(court_id__in=court_ids, valid_from__lte=date_to)
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=date_from))
        .prefetch_related("days")
    )
    override_rows = list(
        ScheduleOverride.objects.filter(court_id__in=court_ids, date__range=(date_from, date_to)).order_by("date")
    )
    overrides = {(o.court_id, o.date): o.day_template_id for o in override_rows}
    template_ids = {d.day_template_id for v in versions for d in v.days.all()}
    template_ids |= {tid for tid in overrides.values() if tid}
    slots_by_template: dict[UUID, list[Slot]] = defaultdict(list)
    for template_id, start, end in (
        DayTemplateSlot.objects.filter(template_id__in=template_ids)
        .order_by("start_time")
        .values_list("template_id", "start_time", "end_time")
    ):
        slots_by_template[template_id].append((start, end))

    versions_by_court: dict[UUID, list[CourtSchedule]] = defaultdict(list)
    for version in versions:
        versions_by_court[version.court_id].append(version)

    resolved: dict[UUID, dict[date, list[Slot]]] = {}
    for court_id in court_ids:
        per_day: dict[date, list[Slot]] = {}
        day = date_from
        while day <= date_to:
            if (court_id, day) in overrides:
                template_id = overrides[(court_id, day)]
            else:
                version = next((v for v in versions_by_court[court_id] if v.covers(day)), None)
                template_id = next(
                    (d.day_template_id for d in version.days.all() if d.weekday == day.weekday()), None
                ) if version else None
            per_day[day] = list(slots_by_template[template_id]) if template_id else []
            day += timedelta(days=1)
        resolved[court_id] = per_day
    overrides_by_court: dict[UUID, list[ScheduleOverride]] = defaultdict(list)
    for override in override_rows:
        overrides_by_court[override.court_id].append(override)
    return Resolved(slots=resolved, overrides=overrides_by_court)


def day_slots(court: Court, day: date) -> list[Slot]:
    return resolve_slots([court], day, day)[court.pk][day]


def describe_override(override: ScheduleOverride) -> str:
    """What members are told about an override, e.g. "Tournament: Junior club championship"."""
    label = override.get_type_display()
    return f"{label}: {override.notes}" if override.notes else label
