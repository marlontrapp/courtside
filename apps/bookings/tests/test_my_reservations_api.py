"""API tests for GET /api/v1/reservations/ ("my bookings")."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings.models import Reservation
from apps.common.tests.factories import (
    create_club,
    create_club_membership,
    create_court,
    create_members_only_club,
    create_reservation,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import ClubMembership

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
LISBON = ZoneInfo("Europe/Lisbon")
TODAY = date(2030, 1, 7)  # Monday; "now" is 12:00 in São Paulo
YESTERDAY, TOMORROW = TODAY - timedelta(days=1), TODAY + timedelta(days=1)
URL = reverse("bookings:reservation-list")
S = Reservation.Status


def at(day: date, hour: int, tz=SAO_PAULO) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=tz)


class MyReservationsTestCase(FrozenTimeMixin, APITestCase):
    frozen_now = at(TODAY, 12)

    @classmethod
    def setUpTestData(cls):
        cls.me = create_user(email="me@example.com")
        cls.friend = create_user(email="friend@example.com")
        cls.stranger = create_user(email="stranger@example.com")

        cls.alpha = create_club(name="Alpha")
        cls.beta = create_club(name="Beta")
        cls.lisboa = create_club(name="Lisboa", timezone="Europe/Lisbon")
        cls.a1 = create_court(club=cls.alpha, name="A1")
        cls.a2 = create_court(club=cls.alpha, name="A2")
        cls.b1 = create_court(club=cls.beta, name="B1")
        cls.l1 = create_court(club=cls.lisboa, name="L1")

        def book(name, court, start, end, status=S.CONFIRMED, owner=None, guests=(), **extra):
            r = create_reservation(
                court=court, primary_player=owner or cls.me,
                start_datetime=start, end_datetime=end, status=status, notes=name, **extra,
            )
            r.guests.set(guests)
            setattr(cls, name, r)

        book("upcoming", cls.a1, at(TOMORROW, 18), at(TOMORROW, 19), guests=[cls.friend])
        book("pending", cls.a2, at(TOMORROW, 10), at(TOMORROW, 11), status=S.PENDING_PAYMENT)
        book("in_progress", cls.a2, at(TODAY, 11), at(TODAY, 13))
        book("late", cls.a1, at(TODAY, 21), at(TODAY, 22))  # 00:00-01:00 UTC *tomorrow*
        book("marked_completed", cls.a1, at(YESTERDAY, 10), at(YESTERDAY, 11), status=S.COMPLETED)
        book("played", cls.a2, at(YESTERDAY, 18), at(YESTERDAY, 19))  # CONFIRMED, in the past
        book("pending_past", cls.a1, at(YESTERDAY, 8), at(YESTERDAY, 9), status=S.PENDING_PAYMENT)
        book("cancelled", cls.a1, at(TOMORROW + timedelta(days=1), 9),
             at(TOMORROW + timedelta(days=1), 10), status=S.CANCELLED)
        book("expired", cls.a2, at(TOMORROW + timedelta(days=2), 9),
             at(TOMORROW + timedelta(days=2), 10), status=S.EXPIRED)
        book("as_guest", cls.b1, at(TOMORROW, 20), at(TOMORROW, 21), owner=cls.friend,
             guests=[cls.me], booking_type=Reservation.BookingType.DOUBLES)
        book("lisbon", cls.l1, at(TOMORROW, 10, LISBON), at(TOMORROW, 11, LISBON),
             booking_type=Reservation.BookingType.LESSON)
        book("not_mine", cls.b1, at(TOMORROW, 12), at(TOMORROW, 13), owner=cls.stranger,
             guests=[cls.friend])

        cls.mine = [
            "upcoming", "pending", "in_progress", "late", "marked_completed", "played",
            "pending_past", "cancelled", "expired", "as_guest", "lisbon",
        ]

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.me)

    def get(self, **params):
        return self.client.get(URL, params)

    def names(self, response=None, **params) -> set[str]:
        response = response or self.get(**params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {item["notes"] for item in response.data["results"]}

    def item(self, name: str, **params) -> dict:
        results = self.get(**params).data["results"]
        return next(r for r in results if r["notes"] == name)


class ScopeTests(MyReservationsTestCase):
    def test_requires_authentication(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.get().status_code, status.HTTP_403_FORBIDDEN)

    def test_lists_my_bookings_as_booker_and_guest_only(self):
        response = self.get()

        self.assertEqual(self.names(response), set(self.mine))
        self.assertEqual(response.data["count"], len(self.mine))

    def test_booking_with_several_guests_is_listed_once(self):
        third = create_user()
        self.upcoming.guests.add(third)  # me (owner) + friend + third
        self.as_guest.guests.add(third)  # friend (owner) + me + third

        response = self.get()

        names = [item["notes"] for item in response.data["results"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(response.data["count"], len(self.mine))

    def test_role(self):
        self.assertEqual(self.item("upcoming")["role"], "owner")
        self.assertEqual(self.item("as_guest")["role"], "guest")
        self.assertEqual(self.names(role="guest"), {"as_guest"})
        self.assertEqual(self.names(role="owner"), set(self.mine) - {"as_guest"})

    def test_still_listed_after_losing_club_membership(self):
        private = create_members_only_club()
        membership = create_club_membership(user=self.me, club=private)
        create_reservation(
            court=create_court(club=private), primary_player=self.me,
            start_datetime=at(TOMORROW, 8), end_datetime=at(TOMORROW, 9), notes="private",
        )
        membership.status = ClubMembership.Status.CANCELLED
        membership.save(update_fields=["status"])

        self.assertIn("private", self.names())


class PayloadTests(MyReservationsTestCase):
    def test_item_shape(self):
        item = self.item("upcoming")

        self.assertEqual(item["start"], "2030-01-08T18:00:00-03:00")
        self.assertEqual(item["end"], "2030-01-08T19:00:00-03:00")
        self.assertEqual(item["status"], "CONFIRMED")
        self.assertEqual(item["state"], "upcoming")
        self.assertEqual(item["primary_player"], "me@example.com")
        self.assertEqual(item["guests"], ["friend@example.com"])
        self.assertEqual(item["court"]["name"], "A1")
        self.assertEqual(item["court"]["club"]["name"], "Alpha")

    def test_lisbon_times_use_club_timezone(self):
        self.assertEqual(self.item("lisbon")["start"], "2030-01-08T10:00:00+00:00")

    def test_post_response_includes_state_and_role(self):
        from apps.common.tests.factories import add_full_week_slots

        court = create_court(club=self.alpha, name="A3")
        add_full_week_slots(court)
        response = self.client.post(
            URL,
            {"court": str(court.id), "date": TOMORROW.isoformat(),
             "start_time": "10:00", "end_time": "11:00"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["state"], "upcoming")
        self.assertEqual(response.data["role"], "owner")


class StateTests(MyReservationsTestCase):
    EXPECTED = {
        "upcoming": "upcoming",
        "in_progress": "upcoming",  # not finished yet
        "late": "upcoming",
        "as_guest": "upcoming",
        "lisbon": "upcoming",
        "pending": "pending_payment",
        "marked_completed": "completed",
        "played": "completed",  # CONFIRMED but already over
        "pending_past": "expired",  # hold whose slot has passed
        "cancelled": "cancelled",
        "expired": "expired",
    }

    def test_derived_state_of_every_booking(self):
        results = self.get().data["results"]

        self.assertEqual({r["notes"]: r["state"] for r in results}, self.EXPECTED)

    def test_filter_by_each_state(self):
        for state in ("upcoming", "pending_payment", "completed", "cancelled", "expired"):
            with self.subTest(state=state):
                expected = {name for name, s in self.EXPECTED.items() if s == state}
                self.assertEqual(self.names(state=state), expected)

    def test_filter_by_several_states(self):
        self.assertEqual(
            self.names(state="cancelled,expired"), {"cancelled", "expired", "pending_past"}
        )

    def test_invalid_state(self):
        response = self.get(state="canceled")  # US spelling is not a value

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("state", response.data)


class DateTimeFilterTests(MyReservationsTestCase):
    def test_date_uses_club_local_date(self):
        # "late" is 21:00 in São Paulo but 00:00 UTC the next day.
        self.assertIn("late", self.names(date=TODAY.isoformat()))
        self.assertNotIn("late", self.names(date=TOMORROW.isoformat()))

    def test_date(self):
        self.assertEqual(
            self.names(date=YESTERDAY.isoformat()), {"marked_completed", "played", "pending_past"}
        )

    def test_open_ended_ranges(self):
        since_tomorrow = self.names(date_from=TOMORROW.isoformat())
        until_today = self.names(date_to=TODAY.isoformat())

        self.assertEqual(
            since_tomorrow, {"upcoming", "pending", "cancelled", "expired", "as_guest", "lisbon"}
        )
        self.assertEqual(since_tomorrow | until_today, set(self.mine))
        self.assertFalse(since_tomorrow & until_today)

    def test_closed_range_is_inclusive(self):
        names = self.names(date_from=TODAY.isoformat(), date_to=TOMORROW.isoformat())

        self.assertEqual(
            names, {"in_progress", "late", "upcoming", "pending", "as_guest", "lisbon"}
        )

    def test_time_window_is_club_local_and_fully_inside(self):
        # 10:00-11:00 local: São Paulo "pending" and Lisbon "lisbon" (07:00 in São Paulo).
        names = self.names(date=TOMORROW.isoformat(), time_from="10:00", time_to="11:00")

        self.assertEqual(names, {"pending", "lisbon"})

    def test_time_from_only(self):
        self.assertEqual(
            self.names(time_from="18:00"), {"upcoming", "late", "played", "as_guest"}
        )

    def test_time_to_only_excludes_bookings_ending_later(self):
        # "in_progress" is 11:00-13:00: it starts before 12:00 but ends after it.
        self.assertEqual(self.names(date=TODAY.isoformat(), time_to="12:00"), set())
        self.assertEqual(self.names(date=TODAY.isoformat(), time_to="13:00"), {"in_progress"})

    def test_validation(self):
        cases = [
            ("date_to", {"date_from": TOMORROW.isoformat(), "date_to": TODAY.isoformat()}),
            ("time_to", {"time_from": "18:00", "time_to": "18:00"}),
            ("date", {"date": "07/01/2030"}),
            ("time_from", {"time_from": "25:00"}),
        ]
        for field, params in cases:
            with self.subTest(params=params):
                response = self.get(**params)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)


class OtherFilterTests(MyReservationsTestCase):
    def test_player_matches_booker_or_guest(self):
        # friend is a guest in "upcoming" and the booker of "as_guest".
        self.assertEqual(self.names(player="friend@example.com"), {"upcoming", "as_guest"})

    def test_player_is_case_insensitive_and_multi_valued(self):
        self.assertEqual(
            self.names(player="FRIEND@example.com,nobody@example.com"), {"upcoming", "as_guest"}
        )

    def test_player_filter_never_reaches_other_peoples_bookings(self):
        self.assertNotIn("not_mine", self.names(player="stranger@example.com"))
        self.assertEqual(self.names(player="stranger@example.com"), set())

    def test_club_and_court(self):
        self.assertEqual(self.names(club=f"{self.beta.id},{self.lisboa.id}"), {"as_guest", "lisbon"})
        self.assertEqual(
            self.names(court=str(self.a2.id)), {"pending", "in_progress", "played", "expired"}
        )

    def test_booking_type(self):
        self.assertEqual(self.names(booking_type="DOUBLES,LESSON"), {"as_guest", "lisbon"})

    def test_filters_combine_with_and(self):
        names = self.names(state="upcoming", club=str(self.alpha.id), time_from="18:00")

        self.assertEqual(names, {"upcoming", "late"})

    def test_invalid_values(self):
        for field, value in (("club", "nope"), ("booking_type", "PICKLEBALL"), ("role", "admin")):
            with self.subTest(field=field):
                response = self.get(**{field: value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)


class OrderingAndPaginationTests(MyReservationsTestCase):
    def starts(self, **params) -> list[str]:
        return [r["start"] for r in self.get(**params).data["results"]]

    def test_default_is_newest_first(self):
        starts = [datetime.fromisoformat(s) for s in self.starts()]

        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_upcoming_soonest_first(self):
        names = [r["notes"] for r in self.get(state="upcoming", ordering="start").data["results"]]

        self.assertEqual(names, ["in_progress", "late", "lisbon", "upcoming", "as_guest"])

    def test_query_count_does_not_grow_with_results(self):
        def query_count() -> int:
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.get().status_code, 200)
            return len(ctx.captured_queries)

        baseline = query_count()
        for hour in range(8, 14):
            r = create_reservation(
                court=self.b1, primary_player=self.me,
                start_datetime=at(TOMORROW + timedelta(days=3), hour),
                end_datetime=at(TOMORROW + timedelta(days=3), hour + 1),
            )
            r.guests.set([self.friend, self.stranger])

        self.assertEqual(query_count(), baseline)
