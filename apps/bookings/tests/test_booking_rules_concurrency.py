"""
Count-based booking rules under concurrency: a player's simultaneous bookings
must not both pass "max N" checks (apps.bookings.rules.lock_players).

Real transactions in two threads (TransactionTestCase: each thread has its
own connection and commits). After counting, each check pauses before the
booking is inserted, so without the lock both would count 0 and both book.
"""

import threading
import time as time_module
from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TransactionTestCase

from apps.bookings import services
from apps.bookings.models import Reservation
from apps.bookings.rules import MaxActiveBookingsKind
from apps.common.tests.factories import (
    add_full_week_slots,
    create_booking_rule,
    create_club,
    create_club_membership,
    create_court,
    create_user,
)
from apps.common.tests.utils import freeze_time

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)
PAUSE_AFTER_COUNTING = 0.5  # seconds


class CountRuleConcurrencyTests(TransactionTestCase):
    def setUp(self):
        freezer = freeze_time(datetime.combine(TODAY, time(9), tzinfo=SP))
        freezer.start()
        self.addCleanup(freezer.stop)
        self.club = create_club()
        self.courts = [create_court(club=self.club, name=f"Court {n}") for n in (1, 2)]
        for court in self.courts:
            add_full_week_slots(court, open_time=time(8), close_time=time(22))
        self.player = create_user()
        create_booking_rule(self.club, "max_active_bookings", {"max": 1}, name="One at a time")

    def book_at_the_same_time(self, bookings=None) -> list[object]:
        """Run `bookings` ((booker, court, guests) each) at the same moment; the player's two bookings by default."""
        bookings = bookings or [(self.player, court, []) for court in self.courts]
        original_check = MaxActiveBookingsKind.check

        def slow_check(kind, rule, attempt):
            result = original_check(kind, rule, attempt)
            time_module.sleep(PAUSE_AFTER_COUNTING)  # the race window: counted, not inserted yet
            return result

        start_together = threading.Barrier(2)
        outcomes: list[object] = []

        def book(booker, court, guests):
            try:
                start_together.wait()
                day = TODAY + timedelta(days=1)
                services.create_reservation(
                    court=court, primary_player=booker, guests=guests,
                    start_datetime=datetime.combine(day, time(18), tzinfo=SP),
                    end_datetime=datetime.combine(day, time(19), tzinfo=SP),
                    status=Reservation.Status.CONFIRMED,
                )
                outcomes.append("booked")
            except services.BookingError as exc:
                outcomes.append(exc.errors)
            finally:
                connection.close()

        with mock.patch.object(MaxActiveBookingsKind, "check", slow_check):
            threads = [threading.Thread(target=book, args=booking) for booking in bookings]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        return outcomes

    def test_only_one_of_two_simultaneous_bookings_passes(self):
        outcomes = self.book_at_the_same_time()

        self.assertEqual(sorted(map(str, outcomes)), sorted([
            "booked", str(["One at a time: max 1 upcoming booking — you already have 1."]),
        ]))
        self.assertEqual(Reservation.objects.filter(primary_player=self.player).count(), 1)

    def test_without_the_lock_both_would_pass(self):
        # Proves the test above can fail: the race is real, the lock is what prevents it.
        with mock.patch("apps.bookings.rules.lock_players"):
            outcomes = self.book_at_the_same_time()

        self.assertEqual(outcomes, ["booked", "booked"])

    def test_a_member_guest_in_two_simultaneous_bookings_is_counted_once_at_a_time(self):
        guest, booker_a, booker_b = create_user(), create_user(), create_user()
        create_club_membership(user=guest, club=self.club)

        outcomes = self.book_at_the_same_time([(booker_a, self.courts[0], [guest]), (booker_b, self.courts[1], [guest])])

        self.assertEqual(sorted(map(str, outcomes)), sorted([
            "booked", str(["One at a time: max 1 upcoming booking — at least one of your guests has already reached it."]),
        ]))

    def test_without_locking_the_guest_both_would_pass(self):
        guest, booker_a, booker_b = create_user(), create_user(), create_user()
        create_club_membership(user=guest, club=self.club)

        with mock.patch("apps.bookings.rules.lock_players"):
            outcomes = self.book_at_the_same_time(
                [(booker_a, self.courts[0], [guest]), (booker_b, self.courts[1], [guest])]
            )

        self.assertEqual(outcomes, ["booked", "booked"])
