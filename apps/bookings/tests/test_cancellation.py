"""Tests for the cancellation policy, service, and POST /reservations/{id}/cancel/."""

from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings.models import Reservation
from apps.bookings.services import (
    CancellationError,
    can_cancel,
    cancel_reservation,
    cancellation_deadline,
    check_cancellation,
    create_reservation as book_via_service,
)
from apps.bookings.tasks import send_booking_cancellation_task
from apps.common.tests.factories import (
    add_full_week_slots,
    create_club,
    create_court,
    create_reservation,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import Club

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)  # Monday; now = 12:00 São Paulo
NOW = datetime.combine(TODAY, time(12), tzinfo=SAO_PAULO)
S = Reservation.Status


def at(days: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(TODAY + timedelta(days=days), time(hour, minute), tzinfo=SAO_PAULO)


class CancellationTestCase(FrozenTimeMixin, TestCase):
    frozen_now = NOW

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_user(email="owner@example.com")
        cls.guest = create_user(email="guest@example.com")
        cls.stranger = create_user(email="stranger@example.com")
        cls.club = create_club(name="Policy Club")  # default 24h window
        cls.court = create_court(club=cls.club)

    def booking(self, start: datetime, status=S.CONFIRMED, hours: int = 1, court=None) -> Reservation:
        reservation = create_reservation(
            court=court or self.court, primary_player=self.owner, status=status,
            start_datetime=start, end_datetime=start + timedelta(hours=hours),
        )
        reservation.guests.add(self.guest)
        return reservation

    def set_window(self, hours: int) -> None:
        Club.objects.filter(pk=self.club.pk).update(cancellation_window_hours=hours)

    def assert_refused(self, reservation, user=None, *, code="not_allowed", text=""):
        reservation = Reservation.objects.select_related("court__club").get(pk=reservation.pk)
        with self.assertRaises(CancellationError) as ctx:
            check_cancellation(reservation, user or self.owner)
        self.assertEqual(ctx.exception.code, code)
        self.assertIn(text, str(ctx.exception))

    def assert_allowed(self, reservation, user=None):
        reservation = Reservation.objects.select_related("court__club").get(pk=reservation.pk)
        check_cancellation(reservation, user or self.owner)  # must not raise


class PolicyTests(CancellationTestCase):
    def test_clubs_default_to_a_24_hour_window(self):
        self.assertEqual(create_club().cancellation_window_hours, 24)
        self.assertEqual(Club._meta.get_field("cancellation_window_hours").default, 24)

    def test_deadline_is_start_minus_window(self):
        reservation = self.booking(at(2, 18))

        self.assertEqual(cancellation_deadline(reservation), at(1, 18))

    def test_confirmed_booking_boundaries(self):
        # now = Mon 12:00, window 24h -> a Tue 12:00 start has its deadline exactly now.
        at_deadline = self.booking(at(1, 12))
        one_second_past = self.booking(at(1, 12) - timedelta(seconds=1), court=create_court(club=self.club))

        self.assert_allowed(at_deadline)
        self.assert_refused(one_second_past, text="deadline has passed")

    def test_deadline_message_mentions_policy_and_local_deadline(self):
        reservation = self.booking(at(0, 18))  # in 6h; deadline was yesterday 18:00

        self.assert_refused(reservation, text="Policy Club allows cancelling up to 24 hours")
        self.assert_refused(reservation, text="deadline was 2030-01-06 18:00")

    def test_zero_window_allows_cancelling_until_start(self):
        self.set_window(0)
        reservation = self.booking(at(0, 12, 1))  # starts in one minute

        self.assert_allowed(reservation)

    def test_window_follows_club_configuration(self):
        self.set_window(48)
        in_30h = self.booking(at(1, 18))

        self.assert_refused(in_30h, text="up to 48 hours")

        self.set_window(6)
        self.assert_allowed(in_30h)

    def test_pending_payment_hold_can_be_cancelled_until_start(self):
        reservation = self.booking(at(0, 13), status=S.PENDING_PAYMENT)  # inside the 24h window

        self.assert_allowed(reservation)

    def test_started_or_finished_bookings(self):
        in_progress = self.booking(at(0, 11), hours=2)
        finished = self.booking(at(-1, 10))

        self.assert_refused(in_progress, text="already started")
        self.assert_refused(finished, text="already started or finished")

    def test_inactive_statuses(self):
        for status_, text in (
            (S.CANCELLED, "already cancelled"),
            (S.EXPIRED, "expired booking"),
            (S.COMPLETED, "completed booking"),
        ):
            with self.subTest(status=status_):
                self.assert_refused(self.booking(at(5, 10), status=status_), text=text)

    def test_only_the_booker_can_cancel(self):
        reservation = self.booking(at(5, 10))

        self.assert_refused(reservation, self.guest, code="forbidden")
        self.assert_refused(reservation, self.stranger, code="forbidden")

    def test_can_cancel_mirrors_check(self):
        reservation = Reservation.objects.select_related("court__club").get(pk=self.booking(at(5, 10)).pk)

        self.assertTrue(can_cancel(reservation, self.owner))
        self.assertFalse(can_cancel(reservation, self.guest))


@mock.patch("apps.bookings.services.send_booking_cancellation_task")
class CancelServiceTests(CancellationTestCase):
    def test_cancels_and_records_audit_fields(self, task):
        reservation = self.booking(at(5, 10))

        with self.captureOnCommitCallbacks(execute=True):
            cancelled = cancel_reservation(reservation, user=self.owner, reason="Rain forecast")
            task.delay.assert_not_called()  # only after commit

        cancelled.refresh_from_db()
        self.assertEqual(cancelled.status, S.CANCELLED)
        self.assertEqual(cancelled.cancelled_at, NOW)
        self.assertEqual(cancelled.cancellation_reason, "Rain forecast")
        task.delay.assert_called_once_with(str(reservation.id))

    def test_refusal_changes_nothing_and_notifies_nobody(self, task):
        reservation = self.booking(at(0, 18))  # past the deadline

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(CancellationError):
                cancel_reservation(reservation, user=self.owner)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, S.CONFIRMED)
        self.assertEqual(callbacks, [])

    def test_is_idempotent(self, task):
        reservation = self.booking(at(5, 10))
        with self.captureOnCommitCallbacks(execute=True):
            first = cancel_reservation(reservation, user=self.owner, reason="first")

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            second = cancel_reservation(reservation, user=self.owner, reason="second")

        self.assertEqual(callbacks, [])  # no second notification
        self.assertEqual(second.cancelled_at, first.cancelled_at)
        self.assertEqual(second.cancellation_reason, "first")
        task.delay.assert_called_once()

    def test_guest_cannot_cancel_even_if_already_cancelled(self, task):
        reservation = self.booking(at(5, 10), status=S.CANCELLED)

        with self.assertRaises(CancellationError) as ctx:
            cancel_reservation(reservation, user=self.guest)
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_cancelling_frees_the_slot(self, task):
        add_full_week_slots(self.court)
        reservation = self.booking(at(5, 10))
        cancel_reservation(reservation, user=self.owner)

        rebooked = book_via_service(
            court=self.court, primary_player=self.stranger,
            start_datetime=at(5, 10), end_datetime=at(5, 11),
        )

        self.assertEqual(rebooked.status, S.PENDING_PAYMENT)


class CancellationTaskTests(CancellationTestCase):
    def test_notifies_booker_and_guests(self):
        reservation = self.booking(at(5, 10), status=S.CANCELLED)

        result = send_booking_cancellation_task(str(reservation.id))

        self.assertEqual(result["notified"], ["owner@example.com", "guest@example.com"])

    def test_missing_reservation(self):
        import uuid

        self.assertEqual(
            send_booking_cancellation_task(str(uuid.uuid4())), {"ok": False, "reason": "not_found"}
        )


class CancelAPITests(FrozenTimeMixin, APITestCase):
    frozen_now = NOW

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_user(email="owner@example.com")
        cls.guest = create_user(email="guest@example.com")
        cls.stranger = create_user(email="stranger@example.com")
        cls.club = create_club(name="Policy Club")
        cls.court = create_court(club=cls.club)
        add_full_week_slots(cls.court)

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.owner)

    def booking(self, start: datetime, status=S.CONFIRMED) -> Reservation:
        reservation = create_reservation(
            court=self.court, primary_player=self.owner, status=status,
            start_datetime=start, end_datetime=start + timedelta(hours=1),
        )
        reservation.guests.add(self.guest)
        return reservation

    def cancel(self, reservation_id, **body):
        return self.client.post(
            reverse("bookings:reservation-cancel", args=[reservation_id]), body, format="json"
        )

    def test_cancels(self):
        reservation = self.booking(at(5, 10))

        response = self.cancel(reservation.id, reason="Injury")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], "CANCELLED")
        self.assertEqual(response.data["state"], "cancelled")
        self.assertEqual(response.data["cancellation_reason"], "Injury")
        self.assertFalse(response.data["can_cancel"])
        self.assertIsNotNone(response.data["cancelled_at"])

    def test_reason_is_optional(self):
        response = self.cancel(self.booking(at(5, 10)).id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["cancellation_reason"], "")

    def test_reason_too_long(self):
        response = self.cancel(self.booking(at(5, 10)).id, reason="x" * 501)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", response.data)

    def test_repeating_the_call_is_safe(self):
        reservation = self.booking(at(5, 10))
        self.cancel(reservation.id)

        response = self.cancel(reservation.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], "cancelled")

    def test_past_deadline_returns_409_with_explanation(self):
        response = self.cancel(self.booking(at(0, 18)).id)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("deadline has passed", response.data["detail"])
        self.assertIn("24 hours", response.data["detail"])

    def test_started_booking_returns_409(self):
        response = self.cancel(self.booking(at(0, 11, 30)).id)  # 11:30-12:30, now 12:00

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_guest_gets_403(self):
        reservation = self.booking(at(5, 10))
        self.client.force_authenticate(self.guest)

        response = self.cancel(reservation.id)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Only the player who made the booking", response.data["detail"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, S.CONFIRMED)

    def test_strangers_and_unknown_ids_get_404(self):
        reservation = self.booking(at(5, 10))
        self.client.force_authenticate(self.stranger)

        self.assertEqual(self.cancel(reservation.id).status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(self.owner)
        self.assertEqual(
            self.cancel("00000000-0000-0000-0000-000000000000").status_code, status.HTTP_404_NOT_FOUND
        )

    def test_requires_authentication(self):
        reservation = self.booking(at(5, 10))
        self.client.force_authenticate(None)

        self.assertEqual(self.cancel(reservation.id).status_code, status.HTTP_403_FORBIDDEN)

    def test_list_shows_policy_fields_per_role(self):
        reservation = self.booking(at(5, 10))
        url = reverse("bookings:reservation-list")

        owner_item = self.client.get(url).data["results"][0]
        self.client.force_authenticate(self.guest)
        guest_item = self.client.get(url).data["results"][0]

        self.assertTrue(owner_item["can_cancel"])
        self.assertFalse(guest_item["can_cancel"])
        self.assertEqual(owner_item["id"], str(reservation.id))
        self.assertEqual(owner_item["cancellation_deadline"], "2030-01-11T10:00:00-03:00")

    def test_cancelled_slot_is_bookable_again(self):
        reservation = self.booking(at(5, 10))
        self.cancel(reservation.id)
        self.client.force_authenticate(self.stranger)

        response = self.client.post(
            reverse("bookings:reservation-list"),
            {"court": str(self.court.id), "date": (TODAY + timedelta(days=5)).isoformat(),
             "start_time": "10:00", "end_time": "11:00"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class ConcurrentCancelTests(TransactionTestCase):
    """Two simultaneous cancels: the row lock makes the second one see the first."""

    def test_second_cancel_waits_for_the_lock_and_does_not_notify_again(self):
        import threading
        from django.db import connection, transaction
        from django.utils import timezone

        owner = create_user()
        court = create_court()
        start = timezone.now() + timedelta(days=5)
        reservation = create_reservation(
            court=court, primary_player=owner, start_datetime=start, end_datetime=start + timedelta(hours=1)
        )
        lock_held, release = threading.Event(), threading.Event()
        outcome = {}

        def first_canceller():  # holds the row lock while "cancelling"
            try:
                with transaction.atomic():
                    Reservation.objects.select_for_update().get(pk=reservation.pk)
                    Reservation.objects.filter(pk=reservation.pk).update(
                        status=S.CANCELLED, cancelled_at=timezone.now()
                    )
                    lock_held.set()
                    release.wait(10)
            finally:
                connection.close()

        def second_canceller():
            try:
                lock_held.wait(10)
                outcome["result"] = cancel_reservation(reservation, user=owner)
            except Exception as exc:  # pragma: no cover - surfaced by the assertions
                outcome["error"] = exc
            finally:
                connection.close()

        with mock.patch("apps.bookings.services.send_booking_cancellation_task") as task:
            first = threading.Thread(target=first_canceller)
            second = threading.Thread(target=second_canceller)
            first.start()
            second.start()
            lock_held.wait(10)
            second.join(0.5)
            self.assertTrue(second.is_alive(), "second cancel should block on the row lock")
            release.set()
            first.join(10)
            second.join(10)

        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"].status, S.CANCELLED)
        task.delay.assert_not_called()  # the second request changed nothing
