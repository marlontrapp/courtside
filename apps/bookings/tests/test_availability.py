"""Tests for slot availability computation."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.bookings.availability import (
    MAX_WINDOW_DAYS,
    AvailabilityWindow,
    TimeSlot,
    get_courts_availability,
    get_courts_slots,
)
from apps.bookings.models import Reservation
from apps.common.tests.factories import (
    add_full_week_slots,
    add_weekly_slots,
    create_club,
    create_court,
    create_reservation,
    create_schedule_override,
)
from apps.common.tests.utils import FrozenTimeMixin

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)  # a Monday
TOMORROW = TODAY + timedelta(days=1)


def at(day: date, hour: int, minute: int = 0, tz: ZoneInfo = SAO_PAULO) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


def starts(slots: list[TimeSlot]) -> list[datetime]:
    return [slot.start for slot in slots]


class AvailabilityTestCase(FrozenTimeMixin, TestCase):
    frozen_now = at(TODAY, 7, 0)

    def setUp(self):
        super().setUp()
        self.court = create_court()
        add_full_week_slots(self.court, open_time=time(8), close_time=time(12))

    def slots_for(self, window: AvailabilityWindow, court=None) -> list[TimeSlot]:
        court = court or self.court
        return get_courts_availability([court], window)[court.id]


class SlotGridTests(AvailabilityTestCase):
    def test_partial_trailing_slot_is_dropped(self):
        court = create_court()
        add_full_week_slots(
            court, open_time=time(8), close_time=time(12), slot_duration_minutes=90
        )

        slots = self.slots_for(AvailabilityWindow.for_date(TOMORROW), court=court)

        # 08:00-09:30, 09:30-11:00; 11:00-12:30 would overrun close_time.
        self.assertEqual(starts(slots), [at(TOMORROW, 8), at(TOMORROW, 9, 30)])

    def test_day_without_slots_has_no_slots(self):
        court = create_court()
        add_weekly_slots(court, [(time(6), time(7))], weekdays=[(TOMORROW.weekday() + 1) % 7])

        self.assertEqual(self.slots_for(AvailabilityWindow.for_date(TOMORROW), court=court), [])

    def test_free_form_slots_lengths_and_gaps(self):
        court = create_court()
        # short early/late slots, long midday ones, a gap 12:00-14:00
        add_weekly_slots(court, [
            (time(7), time(7, 45)), (time(7, 45), time(8, 30)),
            (time(10), time(12)), (time(14), time(16)),
            (time(20), time(20, 45)),
        ])

        slots = self.slots_for(AvailabilityWindow.for_date(TOMORROW), court=court)

        self.assertEqual(slots, [
            TimeSlot(at(TOMORROW, 7), at(TOMORROW, 7, 45)),
            TimeSlot(at(TOMORROW, 7, 45), at(TOMORROW, 8, 30)),
            TimeSlot(at(TOMORROW, 10), at(TOMORROW, 12)),
            TimeSlot(at(TOMORROW, 14), at(TOMORROW, 16)),
            TimeSlot(at(TOMORROW, 20), at(TOMORROW, 20, 45)),
        ])

    def test_each_weekday_uses_its_own_slots(self):
        court = create_court()
        add_weekly_slots(court, [(time(8), time(9))], weekdays=[TOMORROW.weekday()])
        day_after = TOMORROW + timedelta(days=1)
        add_weekly_slots(court, [(time(10), time(11, 30))], weekdays=[day_after.weekday()])

        slots = self.slots_for(AvailabilityWindow(date_from=TOMORROW, date_to=day_after), court=court)

        self.assertEqual(slots, [
            TimeSlot(at(TOMORROW, 8), at(TOMORROW, 9)),
            TimeSlot(at(day_after, 10), at(day_after, 11, 30)),
        ])

    def test_slots_use_club_timezone(self):
        lisbon = ZoneInfo("Europe/Lisbon")
        court = create_court(club=create_club(timezone="Europe/Lisbon"))
        add_full_week_slots(court, open_time=time(8), close_time=time(9))

        slots = self.slots_for(AvailabilityWindow.for_date(TOMORROW), court=court)

        self.assertEqual(slots, [TimeSlot(at(TOMORROW, 8, tz=lisbon), at(TOMORROW, 9, tz=lisbon))])
        self.assertEqual(slots[0].start.isoformat(), "2030-01-08T08:00:00+00:00")


class BusyIntervalTests(AvailabilityTestCase):
    def test_active_reservation_blocks_its_slot(self):
        for status in Reservation.ACTIVE_STATUSES:
            with self.subTest(status=status):
                Reservation.objects.all().delete()
                create_reservation(
                    court=self.court,
                    start_datetime=at(TOMORROW, 9),
                    end_datetime=at(TOMORROW, 10),
                    status=status,
                )

                slots = self.slots_for(AvailabilityWindow.for_date(TOMORROW))

                self.assertNotIn(at(TOMORROW, 9), starts(slots))
                self.assertEqual(len(slots), 3)

    def test_inactive_reservations_do_not_block(self):
        for status in (
            Reservation.Status.CANCELLED,
            Reservation.Status.EXPIRED,
            Reservation.Status.COMPLETED,
        ):
            create_reservation(
                court=self.court,
                start_datetime=at(TOMORROW, 9),
                end_datetime=at(TOMORROW, 10),
                status=status,
            )

        self.assertEqual(len(self.slots_for(AvailabilityWindow.for_date(TOMORROW))), 4)

    def test_misaligned_reservation_blocks_every_overlapped_slot(self):
        create_reservation(
            court=self.court,
            start_datetime=at(TOMORROW, 9, 30),
            end_datetime=at(TOMORROW, 10, 30),
        )

        slots = self.slots_for(AvailabilityWindow.for_date(TOMORROW))

        self.assertEqual(starts(slots), [at(TOMORROW, 8), at(TOMORROW, 11)])

    def test_notices_only_for_overrides_in_the_window(self):
        create_schedule_override(self.court, TOMORROW + timedelta(days=3), None, type="WEATHER")

        result = get_courts_slots([self.court], AvailabilityWindow.for_date(TOMORROW))[self.court.id]

        self.assertEqual(result.notices, [])

    def test_other_courts_bookings_do_not_interfere(self):
        other = create_court()
        create_reservation(
            court=other, start_datetime=at(TOMORROW, 8), end_datetime=at(TOMORROW, 12)
        )

        self.assertEqual(len(self.slots_for(AvailabilityWindow.for_date(TOMORROW))), 4)

    def test_adjacent_bookings_do_not_block(self):
        create_reservation(
            court=self.court, start_datetime=at(TOMORROW, 7), end_datetime=at(TOMORROW, 8)
        )
        create_reservation(
            court=self.court, start_datetime=at(TOMORROW, 12), end_datetime=at(TOMORROW, 13)
        )

        self.assertEqual(len(self.slots_for(AvailabilityWindow.for_date(TOMORROW))), 4)


class WindowTests(AvailabilityTestCase):
    frozen_now = at(TODAY, 9, 30)

    def test_today_excludes_slots_that_already_started(self):
        slots = self.slots_for(AvailabilityWindow.for_date(TODAY))

        self.assertEqual(starts(slots), [at(TODAY, 10), at(TODAY, 11)])

    def test_time_window_keeps_only_slots_fully_inside(self):
        window = AvailabilityWindow.for_date(TOMORROW, time_from=time(8, 30), time_to=time(11, 30))

        slots = self.slots_for(window)

        self.assertEqual(starts(slots), [at(TOMORROW, 9), at(TOMORROW, 10)])

    def test_open_ended_time_window(self):
        only_from = AvailabilityWindow.for_date(TOMORROW, time_from=time(10))
        only_to = AvailabilityWindow.for_date(TOMORROW, time_to=time(10))

        self.assertEqual(starts(self.slots_for(only_from)), [at(TOMORROW, 10), at(TOMORROW, 11)])
        self.assertEqual(starts(self.slots_for(only_to)), [at(TOMORROW, 8), at(TOMORROW, 9)])

    def test_multi_day_range_applies_time_window_each_day(self):
        window = AvailabilityWindow(
            date_from=TOMORROW,
            date_to=TOMORROW + timedelta(days=2),
            time_from=time(11),
        )

        slots = self.slots_for(window)

        self.assertEqual(
            starts(slots),
            [at(TOMORROW + timedelta(days=d), 11) for d in range(3)],
        )

class AvailabilityWindowValidationTests(TestCase):
    def test_rejects_reversed_dates(self):
        with self.assertRaises(ValueError):
            AvailabilityWindow(date_from=TOMORROW, date_to=TODAY)

    def test_rejects_more_than_max_days(self):
        with self.assertRaises(ValueError):
            AvailabilityWindow(date_from=TODAY, date_to=TODAY + timedelta(days=MAX_WINDOW_DAYS))

    def test_rejects_reversed_times(self):
        with self.assertRaises(ValueError):
            AvailabilityWindow.for_date(TODAY, time_from=time(10), time_to=time(9))


class QueryCountTests(AvailabilityTestCase):
    def test_query_count_is_independent_of_window_length(self):
        court = create_court()
        add_full_week_slots(court)
        court = type(court).objects.select_related("club").get(pk=court.pk)

        with CaptureQueriesContext(connection) as one_day:
            get_courts_availability([court], AvailabilityWindow.for_date(TOMORROW))
        with CaptureQueriesContext(connection) as week:
            get_courts_availability([court], AvailabilityWindow(date_from=TOMORROW, date_to=TOMORROW + timedelta(days=6)))

        self.assertEqual(len(week.captured_queries), len(one_day.captured_queries))

    def test_empty_input_runs_no_queries(self):
        with self.assertNumQueries(0):
            self.assertEqual(get_courts_availability([], AvailabilityWindow.for_date(TODAY)), {})
