"""Tests for Celery tasks.

Tasks are invoked directly (synchronously) — broker behaviour is Celery's
concern, ours is the task body. `config.settings_test` also sets
CELERY_TASK_ALWAYS_EAGER so any `.delay()` reached indirectly runs inline.
"""

import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.bookings.models import Reservation
from apps.bookings.tasks import (
    expire_unpaid_reservations_task,
    send_booking_confirmation_task,
    send_upcoming_match_reminder_task,
)
from apps.common.tests.factories import create_reservation, create_user


class SendBookingConfirmationTaskTests(TestCase):
    def test_returns_ok_payload_for_existing_reservation(self):
        reservation = create_reservation()

        result = send_booking_confirmation_task(str(reservation.id))

        self.assertEqual(
            result,
            {
                "ok": True,
                "reservation_id": str(reservation.id),
                "notified": reservation.primary_player.email,
            },
        )

    def test_missing_reservation_is_handled_gracefully(self):
        result = send_booking_confirmation_task(str(uuid.uuid4()))

        self.assertEqual(result, {"ok": False, "reason": "not_found"})


class ExpireUnpaidReservationsTaskTests(TestCase):
    def test_expires_stale_pending_holds(self):
        stale = create_reservation(status=Reservation.Status.PENDING_PAYMENT)
        Reservation.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )

        result = expire_unpaid_reservations_task()

        self.assertEqual(result, {"expired": 1})
        stale.refresh_from_db()
        self.assertEqual(stale.status, Reservation.Status.EXPIRED)

    def test_noop_when_nothing_is_stale(self):
        create_reservation(status=Reservation.Status.PENDING_PAYMENT)

        result = expire_unpaid_reservations_task()

        self.assertEqual(result, {"expired": 0})


class SendUpcomingMatchReminderTaskTests(TestCase):
    def _reservation_starting_in(self, minutes: int, **extra) -> Reservation:
        start = timezone.now() + timedelta(minutes=minutes)
        return create_reservation(
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            **extra,
        )

    def test_reminds_confirmed_reservations_within_window(self):
        upcoming = self._reservation_starting_in(60, status=Reservation.Status.CONFIRMED)
        guest = create_user()
        upcoming.guests.add(guest)

        result = send_upcoming_match_reminder_task()

        self.assertEqual(result, {"reminded": 2, "reservations": 1})

    def test_ignores_reservations_outside_window(self):
        self._reservation_starting_in(60 * 24, status=Reservation.Status.CONFIRMED)

        result = send_upcoming_match_reminder_task()

        self.assertEqual(result, {"reminded": 0, "reservations": 0})

    def test_ignores_non_confirmed_reservations(self):
        self._reservation_starting_in(60, status=Reservation.Status.PENDING_PAYMENT)

        result = send_upcoming_match_reminder_task()

        self.assertEqual(result, {"reminded": 0, "reservations": 0})
