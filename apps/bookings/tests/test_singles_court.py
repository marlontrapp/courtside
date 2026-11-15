"""Singles courts (Court.is_singles_only): no doubles, at most 2 players — a court property, not a rule."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings import services
from apps.bookings.models import BookingRule, Reservation
from apps.bookings.services import BookingError
from apps.common.tests.factories import add_full_week_slots, create_club, create_court, create_user
from apps.common.tests.utils import FrozenTimeMixin

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)
TUESDAY = date(2030, 1, 8)


class SinglesCourtTests(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 9, 30, tzinfo=SP)

    @classmethod
    def setUpTestData(cls):
        cls.club = create_club()
        cls.singles = create_court(club=cls.club, name="Singles court", is_singles_only=True)
        cls.regular = create_court(club=cls.club, name="Court 1")
        for court in (cls.singles, cls.regular):
            add_full_week_slots(court, open_time=time(8), close_time=time(22))
        cls.booker, cls.friend, cls.third, cls.fourth = (create_user() for _ in range(4))

    def book(self, court=None, *, guests=(), booking_type="SINGLES", hour=18):
        return services.create_reservation(
            court=court or self.singles, primary_player=self.booker, guests=list(guests), booking_type=booking_type,
            start_datetime=datetime.combine(TUESDAY, time(hour), tzinfo=SP),
            end_datetime=datetime.combine(TUESDAY, time(hour + 1), tzinfo=SP),
        )

    def rejected(self, **kwargs) -> list[str]:
        with self.assertRaises(BookingError) as ctx:
            self.book(**kwargs)
        return ctx.exception.errors

    def test_no_booking_rules_involved(self):
        self.assertFalse(BookingRule.objects.exists())

    def test_up_to_two_players(self):
        self.book(hour=8)  # alone
        self.book(hour=9, guests=[self.friend])  # the booker + 1 guest
        self.book(hour=10, booking_type="LESSON", guests=[self.friend])

    def test_more_than_two_players(self):
        self.assertEqual(
            self.rejected(guests=[self.friend, self.third]),
            ["This is a singles court: at most 2 players (you and 1 guest)."],
        )

    def test_no_doubles(self):
        self.assertEqual(self.rejected(booking_type="DOUBLES"), ["This is a singles court: doubles can't be booked here."])

    def test_both_problems_are_reported(self):
        self.assertEqual(len(self.rejected(booking_type="DOUBLES", guests=[self.friend, self.third, self.fourth])), 2)
        self.assertFalse(Reservation.objects.exists())

    def test_regular_courts_are_unaffected(self):
        self.book(self.regular, booking_type="DOUBLES", guests=[self.friend, self.third, self.fourth])

    def test_api(self):
        self.client.force_authenticate(self.booker)
        url = reverse("bookings:reservation-list")
        body = {"court": str(self.singles.pk), "date": TUESDAY.isoformat(), "start_time": "18:00", "end_time": "19:00"}

        too_many = self.client.post(url, {**body, "guests": [str(self.friend.pk), str(self.third.pk)]}, format="json")
        self.assertEqual(too_many.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(too_many.data, {"non_field_errors": ["This is a singles court: at most 2 players (you and 1 guest)."]})

        # The same guest by email and by id is one guest: 2 players.
        booked = self.client.post(url, {**body, "guests": [self.friend.email, str(self.friend.pk)]}, format="json")
        self.assertEqual(booked.status_code, status.HTTP_201_CREATED, booked.data)
        self.assertTrue(booked.data["court"]["is_singles_only"])

    def test_shown_in_the_courts_listing(self):
        self.client.force_authenticate(self.booker)

        response = self.client.get(reverse("courts:court-list"), {"date": TUESDAY.isoformat()})

        flags = {court["name"]: court["is_singles_only"] for court in response.data["results"]}
        self.assertEqual(flags, {"Singles court": True, "Court 1": False})
