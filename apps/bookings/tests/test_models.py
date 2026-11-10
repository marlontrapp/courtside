"""Tests for the Reservation model, including the PostgreSQL overlap guard."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.bookings.models import Reservation
from apps.common.tests.factories import (
    create_court,
    create_reservation,
    create_user,
    future_slot,
)


class ReservationValidationTests(TestCase):
    def test_clean_rejects_end_before_start(self):
        start, end = future_slot()
        reservation = Reservation(
            court=create_court(),
            primary_player=create_user(),
            start_datetime=end,
            end_datetime=start,
        )

        with self.assertRaises(ValidationError) as ctx:
            reservation.full_clean()
        self.assertIn("end_datetime", ctx.exception.message_dict)

    def test_db_constraint_rejects_end_before_start(self):
        start, end = future_slot()

        with self.assertRaises(IntegrityError), transaction.atomic():
            create_reservation(start_datetime=end, end_datetime=start)


class ReservationStatusTests(TestCase):
    def test_is_active_for_active_statuses(self):
        for status in Reservation.ACTIVE_STATUSES:
            with self.subTest(status=status):
                self.assertTrue(create_reservation(status=status).is_active)

    def test_is_not_active_for_terminal_statuses(self):
        terminal = (
            Reservation.Status.CANCELLED,
            Reservation.Status.COMPLETED,
            Reservation.Status.EXPIRED,
        )
        for status in terminal:
            with self.subTest(status=status):
                self.assertFalse(create_reservation(status=status).is_active)


class ReservationOverlapConstraintTests(TestCase):
    """The ExclusionConstraint is the DB-level concurrency backstop."""

    def setUp(self):
        self.court = create_court()
        self.start, self.end = future_slot()

    def test_overlapping_active_reservations_rejected(self):
        create_reservation(
            court=self.court,
            start_datetime=self.start,
            end_datetime=self.end,
            status=Reservation.Status.CONFIRMED,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            create_reservation(
                court=self.court,
                start_datetime=self.start + timedelta(minutes=30),
                end_datetime=self.end + timedelta(minutes=30),
                status=Reservation.Status.PENDING_PAYMENT,
            )

    def test_overlap_allowed_when_existing_is_cancelled(self):
        create_reservation(
            court=self.court,
            start_datetime=self.start,
            end_datetime=self.end,
            status=Reservation.Status.CANCELLED,
        )

        reservation = create_reservation(
            court=self.court,
            start_datetime=self.start,
            end_datetime=self.end,
            status=Reservation.Status.CONFIRMED,
        )

        self.assertIsNotNone(reservation.pk)

    def test_overlap_allowed_on_different_court(self):
        create_reservation(
            court=self.court,
            start_datetime=self.start,
            end_datetime=self.end,
        )

        reservation = create_reservation(
            court=create_court(),
            start_datetime=self.start,
            end_datetime=self.end,
        )

        self.assertIsNotNone(reservation.pk)

    def test_back_to_back_slots_allowed(self):
        create_reservation(
            court=self.court,
            start_datetime=self.start,
            end_datetime=self.end,
        )

        reservation = create_reservation(
            court=self.court,
            start_datetime=self.end,
            end_datetime=self.end + timedelta(hours=1),
        )

        self.assertIsNotNone(reservation.pk)


class ReservationStateTests(TestCase):
    """SQL annotation (with_state) and Python (state_at) must always agree."""

    def test_sql_and_python_state_agree_for_every_status_and_time(self):
        from django.utils import timezone

        now = timezone.now()
        windows = {  # (start, end) offsets from now, in hours
            "future": (24, 25),
            "in_progress": (-1, 1),
            "ends_exactly_now": (-1, 0),
            "past": (-25, -24),
        }
        created = [
            create_reservation(
                court=create_court(),  # own court each: no overlap constraints to dodge
                start_datetime=now + timedelta(hours=start_h),
                end_datetime=now + timedelta(hours=end_h),
                status=status,
            )
            for status in Reservation.Status
            for start_h, end_h in windows.values()
        ]

        annotated = {r.pk: r.state for r in Reservation.objects.with_state(now)}
        for reservation in created:
            with self.subTest(status=reservation.status, end=reservation.end_datetime - now):
                self.assertEqual(annotated[reservation.pk], reservation.state_at(now))

    def test_state_examples(self):
        from django.utils import timezone

        now = timezone.now()
        past = {"start_datetime": now - timedelta(hours=2), "end_datetime": now - timedelta(hours=1)}
        future = {"start_datetime": now + timedelta(hours=1), "end_datetime": now + timedelta(hours=2)}
        S, St = Reservation.Status, Reservation.State
        cases = [
            (S.CONFIRMED, future, St.UPCOMING),
            (S.CONFIRMED, past, St.COMPLETED),
            (S.PENDING_PAYMENT, future, St.PENDING_PAYMENT),
            (S.PENDING_PAYMENT, past, St.EXPIRED),
            (S.CANCELLED, future, St.CANCELLED),
        ]
        for status, window, expected in cases:
            with self.subTest(status=status, expected=expected):
                self.assertEqual(Reservation(status=status, **window).state_at(now), expected)
