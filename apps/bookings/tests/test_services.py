"""Tests for the booking service layer (validation, pricing, lifecycle)."""

from datetime import time, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.bookings.models import Reservation
from apps.bookings.services import (
    BookingError,
    SlotValidationResult,
    calculate_total_price,
    confirm_reservation,
    create_reservation,
    expire_stale_pending_reservations,
    validate_slot,
)
from apps.common.tests.factories import (
    add_full_week_slots,
    add_weekly_slots,
    create_club_membership,
    create_court,
    create_day_template,
    create_members_only_club,
    create_reservation as create_reservation_row,
    create_schedule_override,
    create_user,
    future_slot,
)
from apps.courts.models import ClubMembership, ScheduleOverride


class CalculateTotalPriceTests(TestCase):
    def setUp(self):
        self.court = create_court(
            hourly_rate=Decimal("100.00"),
            lighting_fee=Decimal("20.00"),
        )

    def test_price_without_lighting(self):
        start, end = future_slot(duration_minutes=90)

        total = calculate_total_price(self.court, start, end, includes_lighting=False)

        self.assertEqual(total, Decimal("150.00"))

    def test_price_with_lighting(self):
        start, end = future_slot(duration_minutes=60)

        total = calculate_total_price(self.court, start, end, includes_lighting=True)

        self.assertEqual(total, Decimal("120.00"))


class ValidateSlotTests(TestCase):
    def setUp(self):
        self.court = create_court()
        add_full_week_slots(self.court)

    def test_valid_slot_passes(self):
        start, end = future_slot()

        result = validate_slot(self.court, start, end)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.errors, [])

    def test_end_before_start_fails(self):
        start, end = future_slot()

        result = validate_slot(self.court, end, start)

        self.assertFalse(result.is_valid)
        self.assertIn("End datetime must be after start datetime.", result.errors)

    def test_past_slot_fails(self):
        start, end = future_slot()
        past_start = start - timedelta(days=30)
        past_end = end - timedelta(days=30)

        result = validate_slot(self.court, past_start, past_end)

        self.assertFalse(result.is_valid)
        self.assertIn("Cannot book a slot in the past.", result.errors)

    def test_inactive_court_fails(self):
        self.court.is_active = False
        self.court.save(update_fields=["is_active"])
        start, end = future_slot()

        result = validate_slot(self.court, start, end)

        self.assertIn("Court is not active.", result.errors)

    def test_inactive_club_fails(self):
        self.court.club.is_active = False
        self.court.club.save(update_fields=["is_active"])
        start, end = future_slot()

        result = validate_slot(self.court, start, end)

        self.assertIn("Club is not active.", result.errors)

    def test_missing_slots_fails(self):
        court = create_court()  # no weekly slots at all
        start, end = future_slot()

        result = validate_slot(court, start, end)

        self.assertIn("Court has no time slots on this day.", result.errors)

    def test_booking_must_end_on_a_slot_end(self):
        start, end = future_slot(duration_minutes=90)  # slots are 60 minutes

        result = validate_slot(self.court, start, end)

        self.assertTrue(any("must end when one of the court's slots ends" in e for e in result.errors))

    def test_day_closed_by_an_override_says_why(self):
        start, end = future_slot()
        create_schedule_override(
            self.court, start.astimezone(ZoneInfo(self.court.club.timezone)).date(), None,
            type="MAINTENANCE", notes="Clay watering & rolling",
        )

        result = validate_slot(self.court, start, end)

        self.assertIn("Court has no time slots on this day — Maintenance: Clay watering & rolling.", result.errors)

    def test_slot_removed_by_an_override_says_why(self):
        start, end = future_slot(hour=10)
        afternoon = create_day_template(self.court.club, [(time(14), time(15)), (time(15), time(16))])
        create_schedule_override(
            self.court, start.astimezone(ZoneInfo(self.court.club.timezone)).date(), afternoon,
            type=ScheduleOverride.Type.TOURNAMENT, notes="Club championship until 14:00",
        )

        error = validate_slot(self.court, start, end).errors[0]

        self.assertTrue(error.startswith("Booking must start when one of the court's slots starts (that day: 14:00–15:00"), error)
        self.assertTrue(error.endswith(") — Tournament: Club championship until 14:00."), error)

    def test_override_without_notes_still_names_its_type(self):
        start, end = future_slot()
        create_schedule_override(self.court, start.astimezone(ZoneInfo(self.court.club.timezone)).date(), None, type="WEATHER")

        self.assertIn("Court has no time slots on this day — Weather.", validate_slot(self.court, start, end).errors)

    def test_overlapping_active_reservation_fails(self):
        start, end = future_slot()
        create_reservation_row(court=self.court, start_datetime=start, end_datetime=end)

        result = validate_slot(self.court, start, end)

        self.assertIn("Court already has an overlapping active reservation.", result.errors)

    def test_exclude_reservation_id_ignores_own_booking(self):
        start, end = future_slot()
        existing = create_reservation_row(
            court=self.court, start_datetime=start, end_datetime=end
        )

        result = validate_slot(
            self.court, start, end, exclude_reservation_id=existing.pk
        )

        self.assertTrue(result.is_valid)


class ValidateSlotGridTests(TestCase):
    """Bookings must cover whole consecutive slots, as shown by the availability API."""

    def setUp(self):
        self.court = create_court()
        add_full_week_slots(self.court)  # 06:00-23:00, 60-minute slots

    def test_misaligned_start_is_rejected(self):
        start, _ = future_slot(hour=10)
        start += timedelta(minutes=30)

        result = validate_slot(self.court, start, start + timedelta(hours=1))

        self.assertTrue(any("must start when one of the court's slots starts" in e for e in result.errors), result.errors)

    def test_seconds_offset_is_rejected(self):
        start, _ = future_slot(hour=10)
        start += timedelta(seconds=30)

        result = validate_slot(self.court, start, start + timedelta(hours=1))

        self.assertFalse(result.is_valid)

    def test_booking_across_midnight_is_rejected(self):
        start, _ = future_slot(hour=22)

        result = validate_slot(self.court, start, start + timedelta(hours=3))

        self.assertIn("Bookings can't cross midnight.", result.errors)

    def test_error_lists_the_days_slots(self):
        start, _ = future_slot(hour=10)
        start += timedelta(minutes=30)

        error = validate_slot(self.court, start, start + timedelta(hours=1)).errors[0]

        self.assertIn("that day: 06:00–07:00, 07:00–08:00", error)
        self.assertTrue(error.endswith(", …)."), error)  # 17 slots, only the first few shown

    def test_conflict_flag_only_for_overlapping_reservations(self):
        start, end = future_slot()
        self.assertFalse(validate_slot(self.court, start, end).conflict)

        create_reservation_row(court=self.court, start_datetime=start, end_datetime=end)

        self.assertTrue(validate_slot(self.court, start, end).conflict)


@mock.patch("apps.bookings.services.send_booking_confirmation_task")
class CreateReservationTests(TestCase):
    def setUp(self):
        self.court = create_court(
            hourly_rate=Decimal("100.00"),
            lighting_fee=Decimal("20.00"),
        )
        add_full_week_slots(self.court)
        self.player = create_user()

    def test_creates_pending_reservation_with_price(self, mock_task):
        start, end = future_slot()

        reservation = create_reservation(
            court=self.court,
            primary_player=self.player,
            start_datetime=start,
            end_datetime=end,
        )

        self.assertEqual(reservation.status, Reservation.Status.PENDING_PAYMENT)
        self.assertEqual(reservation.total_price, Decimal("100.00"))
        mock_task.delay.assert_not_called()

    def test_confirmed_reservation_enqueues_notification_after_commit(self, mock_task):
        start, end = future_slot()

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            reservation = create_reservation(
                court=self.court,
                primary_player=self.player,
                start_datetime=start,
                end_datetime=end,
                status=Reservation.Status.CONFIRMED,
            )
            mock_task.delay.assert_not_called()  # not before the transaction commits

        self.assertEqual(len(callbacks), 1)
        mock_task.delay.assert_called_once_with(str(reservation.id))

    def test_failed_booking_enqueues_nothing(self, mock_task):
        start, end = future_slot()

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(BookingError):
                create_reservation(
                    court=self.court,
                    primary_player=self.player,
                    start_datetime=end,
                    end_datetime=start,
                    status=Reservation.Status.CONFIRMED,
                )

        self.assertEqual(callbacks, [])
        mock_task.delay.assert_not_called()

    def test_guests_are_attached(self, mock_task):
        start, end = future_slot()
        guest = create_user()

        reservation = create_reservation(
            court=self.court,
            primary_player=self.player,
            start_datetime=start,
            end_datetime=end,
            guests=[guest],
        )

        self.assertQuerySetEqual(reservation.guests.all(), [guest])

    def test_invalid_slot_raises_booking_error(self, mock_task):
        start, end = future_slot()

        with self.assertRaises(BookingError):
            create_reservation(
                court=self.court,
                primary_player=self.player,
                start_datetime=end,
                end_datetime=start,
            )

    def test_lighting_on_unlit_court_raises(self, mock_task):
        court = create_court(has_lighting=False)
        add_full_week_slots(court)
        start, end = future_slot()

        with self.assertRaisesMessage(BookingError, "does not support lighting"):
            create_reservation(
                court=court,
                primary_player=self.player,
                start_datetime=start,
                end_datetime=end,
                includes_lighting=True,
            )

    def test_overlap_raises_booking_error(self, mock_task):
        start, end = future_slot()
        create_reservation_row(court=self.court, start_datetime=start, end_datetime=end)

        with self.assertRaisesMessage(BookingError, "overlapping active reservation") as ctx:
            create_reservation(
                court=self.court,
                primary_player=self.player,
                start_datetime=start,
                end_datetime=end,
            )
        self.assertTrue(ctx.exception.conflict)

    def test_db_constraint_race_is_reported_as_conflict(self, mock_task):
        """validate_slot passed, but a concurrent booking won: ExclusionConstraint fires."""
        start, end = future_slot()
        create_reservation_row(court=self.court, start_datetime=start, end_datetime=end)

        with mock.patch(
            "apps.bookings.services.validate_slot",
            return_value=SlotValidationResult(is_valid=True, errors=[]),
        ):
            with self.assertRaises(BookingError) as ctx:
                create_reservation(
                    court=self.court,
                    primary_player=self.player,
                    start_datetime=start,
                    end_datetime=end,
                )

        self.assertTrue(ctx.exception.conflict)
        self.assertEqual(Reservation.objects.count(), 1)


@mock.patch("apps.bookings.services.send_booking_confirmation_task")
class CreateReservationMembershipTests(TestCase):
    """Members-only clubs: only active members may book (guests are not checked)."""

    def setUp(self):
        self.club = create_members_only_club()
        self.court = create_court(club=self.club)
        add_full_week_slots(self.court)
        self.player = create_user()
        self.start, self.end = future_slot()

    def _book(self, **extra):
        return create_reservation(
            court=self.court,
            primary_player=self.player,
            start_datetime=self.start,
            end_datetime=self.end,
            **extra,
        )

    def test_non_member_cannot_book(self, mock_task):
        with self.assertRaisesMessage(BookingError, "restricted to club members"):
            self._book()
        self.assertFalse(Reservation.objects.exists())

    def test_active_member_can_book(self, mock_task):
        create_club_membership(user=self.player, club=self.club)

        reservation = self._book()

        self.assertIsNotNone(reservation.pk)

    def test_inactive_membership_cannot_book(self, mock_task):
        for status in (ClubMembership.Status.SUSPENDED, ClubMembership.Status.CANCELLED):
            with self.subTest(status=status):
                ClubMembership.objects.all().delete()
                create_club_membership(user=self.player, club=self.club, status=status)

                with self.assertRaisesMessage(BookingError, "restricted to club members"):
                    self._book()

    def test_member_may_bring_non_member_guest(self, mock_task):
        create_club_membership(user=self.player, club=self.club)
        guest = create_user()

        reservation = self._book(guests=[guest])

        self.assertQuerySetEqual(reservation.guests.all(), [guest])


@mock.patch("apps.bookings.services.send_booking_confirmation_task")
class ConfirmReservationTests(TestCase):
    def test_pending_reservation_is_confirmed(self, mock_task):
        reservation = create_reservation_row(status=Reservation.Status.PENDING_PAYMENT)

        with self.captureOnCommitCallbacks(execute=True):
            confirmed = confirm_reservation(reservation)
            mock_task.delay.assert_not_called()

        confirmed.refresh_from_db()
        self.assertEqual(confirmed.status, Reservation.Status.CONFIRMED)
        mock_task.delay.assert_called_once_with(str(reservation.id))

    def test_non_pending_reservation_raises(self, mock_task):
        reservation = create_reservation_row(status=Reservation.Status.CANCELLED)

        with self.assertRaisesMessage(BookingError, "Only PENDING_PAYMENT"):
            confirm_reservation(reservation)
        mock_task.delay.assert_not_called()


class ExpireStalePendingReservationsTests(TestCase):
    def _backdate(self, reservation, minutes: int):
        """created_at is auto_now_add, so rewrite it via an UPDATE."""
        Reservation.objects.filter(pk=reservation.pk).update(
            created_at=reservation.created_at - timedelta(minutes=minutes)
        )

    def test_expires_only_stale_pending_holds(self):
        stale = create_reservation_row(status=Reservation.Status.PENDING_PAYMENT)
        self._backdate(stale, minutes=60)
        fresh = create_reservation_row(status=Reservation.Status.PENDING_PAYMENT)
        confirmed = create_reservation_row(status=Reservation.Status.CONFIRMED)
        self._backdate(confirmed, minutes=60)

        expired_count = expire_stale_pending_reservations(older_than_minutes=15)

        self.assertEqual(expired_count, 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        confirmed.refresh_from_db()
        self.assertEqual(stale.status, Reservation.Status.EXPIRED)
        self.assertEqual(fresh.status, Reservation.Status.PENDING_PAYMENT)
        self.assertEqual(confirmed.status, Reservation.Status.CONFIRMED)

    def test_uses_settings_hold_window_by_default(self):
        stale = create_reservation_row(status=Reservation.Status.PENDING_PAYMENT)
        self._backdate(stale, minutes=10)

        with self.settings(RESERVATION_HOLD_MINUTES=5):
            expired_count = expire_stale_pending_reservations()

        self.assertEqual(expired_count, 1)


class ValidateSlotFreeFormScheduleTests(TestCase):
    """Slots of different lengths, gaps, and per-weekday schedules."""

    def setUp(self):
        self.court = create_court()
        self.start, _ = future_slot(hour=6)
        self.weekday = self.start.weekday()
        # 06:00-06:45, 06:45-07:30, [gap], 09:00-11:00, 11:00-12:30
        add_weekly_slots(self.court, [
            (time(6), time(6, 45)), (time(6, 45), time(7, 30)),
            (time(9), time(11)), (time(11), time(12, 30)),
        ], weekdays=[self.weekday])

    def at(self, hour, minute=0):
        return self.start.replace(hour=hour, minute=minute)

    def test_consecutive_slots_of_different_lengths(self):
        result = validate_slot(self.court, self.at(9), self.at(12, 30))

        self.assertTrue(result.is_valid, result.errors)

    def test_cannot_span_a_gap(self):
        result = validate_slot(self.court, self.at(6, 45), self.at(11))

        self.assertTrue(any("without a gap" in e for e in result.errors), result.errors)

    def test_cannot_run_past_the_last_slot(self):
        result = validate_slot(self.court, self.at(11), self.at(13))

        self.assertTrue(any("must end when" in e for e in result.errors), result.errors)

    def test_other_weekdays_have_their_own_schedule(self):
        other_day = self.start + timedelta(days=1)
        add_weekly_slots(self.court, [(time(6), time(8))], weekdays=[other_day.weekday()])

        self.assertTrue(validate_slot(self.court, other_day, other_day.replace(hour=8)).is_valid)
        self.assertFalse(validate_slot(self.court, other_day, other_day.replace(hour=6, minute=45)).is_valid)
