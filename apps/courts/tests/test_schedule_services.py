"""
Schedule services: timeline of weekly versions, overrides, day templates,
copying, and protection of future bookings (conflicts / force).

Time is frozen on Monday 2030-01-07 12:00 in São Paulo (the clubs' timezone).
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.bookings.models import Reservation
from apps.common.tests.factories import (
    create_club,
    create_court,
    create_court_schedule,
    create_day_template,
    create_reservation,
    create_schedule_override,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import CourtSchedule, ScheduleOverride
from apps.courts.schedule import END_OF_DAY, day_slots
from apps.courts.services import (
    ScheduleConflict,
    ScheduleError,
    ScheduleInUse,
    copy_schedule,
    delete_override,
    delete_schedule,
    plan_schedule,
    set_override,
    template_locked,
    update_day_template,
)
from apps.courts.services import create_day_template as new_day_template

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)  # Monday
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
WEEKDAYS = [MON, TUE, WED, THU, FRI]


def d(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def t(text: str) -> time:
    return END_OF_DAY if text == "24:00" else time.fromisoformat(text)


def slots(*pairs: str) -> list[tuple[time, time]]:
    return [tuple(t(part) for part in pair.split("-")) for pair in pairs]


def at(day: date, text: str) -> datetime:
    return datetime.combine(day, t(text), tzinfo=SP)


class ScheduleTestCase(FrozenTimeMixin, TestCase):
    frozen_now = datetime(2030, 1, 7, 12, 0, tzinfo=SP)

    def setUp(self):
        super().setUp()
        self.club = create_club()
        self.court = create_court(club=self.club)
        self.hourly = create_day_template(self.club, slots("08:00-09:00", "09:00-10:00", "10:00-11:00"), "Hourly")
        self.long = create_day_template(self.club, slots("08:00-09:30", "09:30-11:00"), "Long")
        self.current = create_court_schedule(self.court, {day: self.hourly for day in range(7)}, valid_from=d(-30))

    def timeline(self, court=None):
        """[(valid_from, valid_until, {weekday: template name})] oldest first."""
        return [
            (v.valid_from, v.valid_until, {x.weekday: x.day_template.name for x in v.days.all()})
            for v in (court or self.court).schedules.order_by("valid_from").prefetch_related("days__day_template")
        ]

    def book(self, day: date, start: str, end: str, court=None, **extra) -> Reservation:
        return create_reservation(
            court=court or self.court, start_datetime=at(day, start), end_datetime=at(day, end), **extra
        )


class PlanScheduleTimelineTests(ScheduleTestCase):
    def test_new_version_cuts_the_current_one_short(self):
        change = plan_schedule(self.court, valid_from=d(7), days={MON: self.long})

        self.assertEqual(self.timeline(), [
            (d(-30), d(6), {day: "Hourly" for day in range(7)}),
            (d(7), None, {**{day: "Hourly" for day in range(7)}, MON: "Long"}),  # other weekdays inherited
        ])
        self.assertEqual(change.result.valid_from, d(7))
        self.assertEqual(change.cancelled, [])

    def test_slots_follow_the_timeline(self):
        plan_schedule(self.court, valid_from=d(7), days={MON: self.long})

        self.assertEqual(day_slots(self.court, d(0)), slots("08:00-09:00", "09:00-10:00", "10:00-11:00"))
        self.assertEqual(day_slots(self.court, d(7)), slots("08:00-09:30", "09:30-11:00"))
        self.assertEqual(day_slots(self.court, d(8)), slots("08:00-09:00", "09:00-10:00", "10:00-11:00"))

    def test_null_closes_a_weekday(self):
        plan_schedule(self.court, valid_from=d(7), days={SUN: None})

        self.assertNotIn(SUN, self.timeline()[-1][2])
        self.assertEqual(day_slots(self.court, d(13)), [])  # a Sunday

    def test_same_start_replaces_a_planned_version(self):
        plan_schedule(self.court, valid_from=d(7), days={MON: self.long})

        plan_schedule(self.court, valid_from=d(7), days={MON: self.hourly, TUE: self.long})

        self.assertEqual(self.timeline()[1], (d(7), None, {**{day: "Hourly" for day in range(7)}, TUE: "Long"}))
        self.assertEqual(len(self.timeline()), 2)

    def test_open_ended_version_stops_before_the_next_planned_one(self):
        plan_schedule(self.court, valid_from=d(14), days={MON: self.long})

        plan_schedule(self.court, valid_from=d(7), days={TUE: self.long})

        self.assertEqual([v[:2] for v in self.timeline()], [(d(-30), d(6)), (d(7), d(13)), (d(14), None)])

    def test_temporary_version_resumes_the_previous_schedule(self):
        plan_schedule(self.court, valid_from=d(7), valid_until=d(20), days={day: self.long for day in range(7)})

        self.assertEqual(self.timeline(), [
            (d(-30), d(6), {day: "Hourly" for day in range(7)}),
            (d(7), d(20), {day: "Long" for day in range(7)}),
            (d(21), None, {day: "Hourly" for day in range(7)}),
        ])

    def test_temporary_version_over_planned_ones_is_rejected(self):
        plan_schedule(self.court, valid_from=d(14), days={MON: self.long})

        with self.assertRaises(ScheduleError) as ctx:
            plan_schedule(self.court, valid_from=d(7), valid_until=d(20), days={MON: self.long})

        self.assertIn("valid_until", ctx.exception.errors)
        self.assertEqual(len(self.timeline()), 2)

    def test_template_of_another_club_is_rejected(self):
        # The API checks this too; the service must on its own (other callers, e.g. the seed).
        foreign = create_day_template(create_club(), slots("08:00-09:00"))

        with self.assertRaises(ScheduleError) as ctx:
            plan_schedule(self.court, valid_from=d(7), days={MON: foreign})

        self.assertIn("day_template", ctx.exception.errors)

    def test_court_without_any_schedule(self):
        court = create_court(club=self.club)

        plan_schedule(court, valid_from=d(1), days={day: self.hourly for day in WEEKDAYS})

        self.assertEqual(self.timeline(court), [(d(1), None, {day: "Hourly" for day in WEEKDAYS})])

    def test_history_is_never_rewritten(self):
        before = day_slots(self.court, d(-5))

        plan_schedule(self.court, valid_from=TODAY, days={day: self.long for day in range(7)})

        self.assertEqual(day_slots(self.court, d(-5)), before)


class DeleteScheduleTests(ScheduleTestCase):
    def test_deleting_a_version_between_two_others(self):
        plan_schedule(self.court, valid_from=d(14), days={MON: self.long})
        middle = plan_schedule(self.court, valid_from=d(7), days={TUE: self.long}).result

        delete_schedule(middle)

        self.assertEqual([v[:2] for v in self.timeline()], [(d(-30), d(13)), (d(14), None)])

    def test_today_is_still_editable(self):
        starting_today = plan_schedule(self.court, valid_from=TODAY, days={MON: self.long}).result

        delete_schedule(starting_today)

        self.assertEqual([v[:2] for v in self.timeline()], [(d(-30), None)])


class BookingProtectionTests(ScheduleTestCase):
    """Future bookings that fit the old slots but not the new ones: 409, or cancelled with force."""

    def test_force_cancels_only_the_bookings_that_no_longer_fit(self):
        broken = self.book(d(7), "09:00", "10:00")
        still_fits = self.book(d(14), "08:00", "11:00")  # whole morning: fits both templates
        other_day = self.book(d(8), "09:00", "10:00")  # Tuesday keeps Hourly

        change = plan_schedule(self.court, valid_from=d(7), days={MON: self.long}, force=True)

        self.assertEqual(change.cancelled, [broken])
        for booking, status in [(broken, "CANCELLED"), (still_fits, "CONFIRMED"), (other_day, "CONFIRMED")]:
            booking.refresh_from_db()
            self.assertEqual(booking.status, status)

    def test_bookings_before_the_change_are_not_affected(self):
        self.book(d(0), "14:00", "15:00")  # today, before the new version starts... and off-schedule anyway
        self.book(d(1), "09:00", "10:00")  # before valid_from

        change = plan_schedule(self.court, valid_from=d(7), days={day: self.long for day in range(7)})

        self.assertEqual(change.cancelled, [])

    def test_started_and_past_bookings_are_ignored(self):
        court = create_court(club=self.club)
        midday = create_day_template(self.club, slots("10:00-11:00", "11:00-13:00"), "Midday")
        create_court_schedule(court, {MON: midday}, valid_from=d(-30))
        past = self.book(TODAY, "10:00", "11:00", court=court)  # already over (it's 12:00)
        in_progress = self.book(TODAY, "11:00", "13:00", court=court)  # fits, but has started

        change = plan_schedule(court, valid_from=TODAY, days={MON: None})  # close Mondays from today

        self.assertEqual(change.cancelled, [])
        for booking in (past, in_progress):
            booking.refresh_from_db()
            self.assertEqual(booking.status, Reservation.Status.CONFIRMED)

    def test_cancelled_bookings_are_ignored(self):
        self.book(d(7), "09:00", "10:00", status=Reservation.Status.CANCELLED)

        plan_schedule(self.court, valid_from=d(7), days={MON: self.long})  # no conflict

    def test_bookings_that_already_did_not_fit_are_not_blamed(self):
        self.book(d(7), "14:00", "15:00")  # off-schedule before and after (e.g. admin-made)

        change = plan_schedule(self.court, valid_from=d(7), days={MON: self.long})

        self.assertEqual(change.cancelled, [])

    def test_a_later_start_date_avoids_the_conflict(self):
        self.book(d(7), "09:00", "10:00")

        plan_schedule(self.court, valid_from=d(8), days={MON: self.long})  # next Monday onwards

        self.assertEqual(day_slots(self.court, d(14)), slots("08:00-09:30", "09:30-11:00"))

    def test_temporary_version_only_checks_its_period(self):
        self.book(d(21), "09:00", "10:00")  # after the temporary period: the old schedule resumes

        change = plan_schedule(self.court, valid_from=d(7), valid_until=d(20), days={MON: self.long})

        self.assertEqual(change.cancelled, [])

class OverrideTests(ScheduleTestCase):
    def test_override_replaces_the_weekly_slots_for_one_date(self):
        set_override(self.court, day=d(2), day_template=self.long, type="TOURNAMENT", notes="Club championship")

        self.assertEqual(day_slots(self.court, d(2)), slots("08:00-09:30", "09:30-11:00"))
        self.assertEqual(day_slots(self.court, d(9)), slots("08:00-09:00", "09:00-10:00", "10:00-11:00"))

    def test_setting_again_replaces_it(self):
        set_override(self.court, day=d(2), day_template=None)
        set_override(self.court, day=d(2), day_template=self.long, type="EVENT", notes="Reopened for the open day")

        override = ScheduleOverride.objects.get()
        self.assertEqual(
            (override.day_template, override.type, override.notes), (self.long, "EVENT", "Reopened for the open day")
        )

    def test_override_takes_precedence_over_a_later_weekly_change(self):
        set_override(self.court, day=d(14), day_template=self.hourly)

        plan_schedule(self.court, valid_from=d(7), days={MON: self.long})

        self.assertEqual(day_slots(self.court, d(14)), slots("08:00-09:00", "09:00-10:00", "10:00-11:00"))

    def test_deleting_checks_bookings_made_under_it(self):
        set_override(self.court, day=d(2), day_template=self.long)
        self.book(d(2), "09:30", "11:00")

        with self.assertRaises(ScheduleConflict):
            delete_override(ScheduleOverride.objects.get())

class DayTemplateTests(ScheduleTestCase):
    def test_overlapping_slots_are_rejected(self):
        # The API checks this too; the service must on its own (other callers, e.g. the seed).
        with self.assertRaises(ScheduleError) as ctx:
            new_day_template(club=self.club, name="Bad", slots=slots("08:00-09:00", "08:30-09:30"))

        self.assertEqual(ctx.exception.errors, {"slots": ["08:00–09:00 overlaps 08:30–09:30."]})

    def test_names_are_unique_per_club_case_insensitive(self):
        with self.assertRaises(ScheduleError):
            new_day_template(club=self.club, name="hourly", slots=[])
        new_day_template(club=create_club(), name="Hourly", slots=[])  # another club: fine

    def test_a_template_that_applied_to_past_days_is_locked(self):
        self.assertTrue(template_locked(self.hourly))

        with self.assertRaises(ScheduleInUse):
            update_day_template(self.hourly, slots=slots("08:00-10:00"))

    def test_unused_planned_or_from_today_templates_can_change(self):
        weekend = create_day_template(self.club, slots("08:00-09:00"), "Weekend")
        self.assertFalse(template_locked(weekend))
        plan_schedule(self.court, valid_from=d(5), days={SAT: weekend})
        self.assertFalse(template_locked(weekend))
        set_override(self.court, day=TODAY, day_template=weekend)
        self.assertFalse(template_locked(weekend))  # today isn't history yet

        update_day_template(weekend, slots=slots("07:00-09:00"))

        self.assertEqual(day_slots(self.court, d(5)), slots("07:00-09:00"))

    def test_changing_a_planned_template_checks_bookings(self):
        weekend = create_day_template(self.club, slots("08:00-09:00"), "Weekend")
        plan_schedule(self.court, valid_from=d(5), days={SAT: weekend})
        booking = self.book(d(5), "08:00", "09:00")

        with self.assertRaises(ScheduleConflict):
            update_day_template(weekend, slots=slots("07:00-09:00"))
        change = update_day_template(weekend, slots=slots("07:00-09:00"), force=True)

        self.assertEqual(change.cancelled, [booking])

    def test_override_in_the_past_locks_it(self):
        template = create_day_template(self.club, slots("08:00-09:00"), "Special")
        create_schedule_override(self.court, d(-1), template)

        self.assertTrue(template_locked(template))

    def test_deleting_the_club_deletes_everything(self):
        set_override(self.court, day=d(2), day_template=self.long)

        self.club.delete()

        self.assertFalse(CourtSchedule.objects.exists())


class CopyScheduleTests(ScheduleTestCase):
    def setUp(self):
        super().setUp()
        plan_schedule(self.court, valid_from=d(1), days={SAT: self.long, SUN: None})
        self.targets = [create_court(club=self.club) for _ in range(3)]

    def test_copies_all_seven_weekdays(self):
        change = copy_schedule(self.court, self.targets, valid_from=d(3))

        expected = {**{day: "Hourly" for day in range(6)}, SAT: "Long"}
        for target in self.targets:
            self.assertEqual(self.timeline(target), [(d(3), None, expected)])
        self.assertEqual(len(change.result), 3)

    def test_replaces_the_targets_schedule_from_that_date(self):
        create_court_schedule(self.targets[0], {day: self.long for day in range(7)}, valid_from=d(-10))

        copy_schedule(self.court, self.targets[:1], valid_from=d(3))

        timeline = self.timeline(self.targets[0])
        self.assertEqual([v[:2] for v in timeline], [(d(-10), d(2)), (d(3), None)])
        self.assertNotIn(SUN, timeline[1][2])  # closed days are copied too

    def test_copies_what_the_source_has_on_valid_from(self):
        copy_schedule(self.court, self.targets[:1], valid_from=TODAY)

        self.assertEqual(self.timeline(self.targets[0])[0][2], {day: "Hourly" for day in range(7)})

    def test_source_cannot_be_a_target(self):
        with self.assertRaises(ScheduleError):
            copy_schedule(self.court, [self.court], valid_from=d(3))

    def test_all_or_nothing_on_conflicts(self):
        create_court_schedule(self.targets[1], {day: self.long for day in range(7)}, valid_from=d(-10))
        booking = self.book(d(3), "09:30", "11:00", court=self.targets[1])  # fits Long only

        with self.assertRaises(ScheduleConflict):
            copy_schedule(self.court, self.targets, valid_from=d(3))
        self.assertFalse(self.targets[0].schedules.exists())  # nothing was copied

        change = copy_schedule(self.court, self.targets, valid_from=d(3), force=True)
        self.assertEqual(change.cancelled, [booking])

    def test_source_without_a_schedule_on_that_date(self):
        with self.assertRaises(ScheduleError):
            copy_schedule(self.targets[0], self.targets[1:], valid_from=d(3))
