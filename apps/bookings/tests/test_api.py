"""API tests for POST /api/v1/reservations/."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings.models import Reservation
from apps.bookings.services import SlotValidationResult
from apps.common.tests.factories import (
    add_full_week_slots,
    create_club,
    create_club_membership,
    create_court,
    create_members_only_club,
    create_reservation,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
LISBON = ZoneInfo("Europe/Lisbon")
TODAY = date(2030, 1, 7)  # Monday
TOMORROW = TODAY + timedelta(days=1)
URL = reverse("bookings:reservation-list")
COURTS_URL = reverse("courts:court-list")


def at(day: date, hour: int, minute: int = 0, tz=SAO_PAULO) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


class ReservationAPITestCase(FrozenTimeMixin, APITestCase):
    frozen_now = at(TODAY, 9, 30)

    @classmethod
    def setUpTestData(cls):
        cls.player = create_user(email="player@example.com")
        cls.friend = create_user(email="friend@example.com")
        cls.club = create_club(name="Open Club")
        cls.court = create_court(
            club=cls.club, name="Clay", hourly_rate=Decimal("100.00"), lighting_fee=Decimal("20.00")
        )
        cls.unlit = create_court(club=cls.club, name="Day Court", has_lighting=False)
        for court in (cls.court, cls.unlit):
            add_full_week_slots(court, open_time=time(8), close_time=time(22))

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.player)

    def book(self, **overrides):
        payload = {
            "court": str(self.court.id),
            "date": TOMORROW.isoformat(),
            "start_time": "18:00",
            "end_time": "19:00",
        }
        payload.update(overrides)
        return self.client.post(URL, payload, format="json")

    def assert_error(self, response, code: int, text: str, field: str = "non_field_errors"):
        self.assertEqual(response.status_code, code, response.data)
        self.assertIn(field, response.data)
        self.assertIn(text, " ".join(str(e) for e in response.data[field]))


class AuthTests(ReservationAPITestCase):
    def test_requires_authentication(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.book().status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Reservation.objects.exists())


class CreateReservationTests(ReservationAPITestCase):
    def test_books_a_slot(self):
        response = self.book(includes_lighting=True, guests=["friend@example.com"], notes="Bring balls")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        body = response.data
        self.assertEqual(body["status"], "CONFIRMED")  # no payment step yet
        self.assertEqual(body["start"], "2030-01-08T18:00:00-03:00")
        self.assertEqual(body["end"], "2030-01-08T19:00:00-03:00")
        self.assertEqual(body["total_price"], "120.00")  # 100 + 20 lighting
        self.assertEqual(body["guests"], ["friend@example.com"])
        self.assertEqual(body["booking_type"], "SINGLES")
        self.assertEqual(body["court"]["id"], str(self.court.id))
        self.assertEqual(body["court"]["club"]["name"], "Open Club")

        reservation = Reservation.objects.get(pk=body["id"])
        self.assertEqual(reservation.primary_player, self.player)
        self.assertEqual(reservation.start_datetime, at(TOMORROW, 18))
        self.assertEqual(reservation.notes, "Bring balls")

    def test_multi_slot_booking(self):
        response = self.book(start_time="18:00", end_time="20:00", booking_type="DOUBLES")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["total_price"], "200.00")

    def test_booked_slot_disappears_from_availability(self):
        self.book()

        listing = self.client.get(COURTS_URL, {"date": TOMORROW.isoformat(), "time_from": "17:00",
                                               "time_to": "20:00"})

        clay = next(c for c in listing.data["results"] if c["name"] == "Clay")
        self.assertEqual(
            [slot["start"] for slot in clay["available_slots"]],
            [at(TOMORROW, 17).isoformat(), at(TOMORROW, 19).isoformat()],
        )

    def test_broker_outage_does_not_fail_a_committed_booking(self):
        with mock.patch("apps.bookings.services.send_booking_confirmation_task") as task:
            task.delay.side_effect = ConnectionError("broker unreachable")
            # robust=True: logged (django.db.backends.base in prod, django.test here), not raised.
            with self.assertLogs("django", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.book()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Reservation.objects.filter(pk=response.data["id"]).exists())

    def test_times_are_in_the_club_timezone(self):
        lisbon_court = create_court(club=create_club(name="Lisboa", timezone="Europe/Lisbon"))
        add_full_week_slots(lisbon_court, open_time=time(8), close_time=time(22))

        response = self.book(court=str(lisbon_court.id), start_time="10:00", end_time="11:00")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["start"], "2030-01-08T10:00:00+00:00")
        self.assertEqual(
            Reservation.objects.get(pk=response.data["id"]).start_datetime, at(TOMORROW, 10, tz=LISBON)
        )

    def test_cancelled_reservation_does_not_block(self):
        create_reservation(
            court=self.court, start_datetime=at(TOMORROW, 18), end_datetime=at(TOMORROW, 19),
            status=Reservation.Status.CANCELLED,
        )

        self.assertEqual(self.book().status_code, status.HTTP_201_CREATED)


class ConflictTests(ReservationAPITestCase):
    def test_taken_slot_returns_409(self):
        create_reservation(court=self.court, start_datetime=at(TOMORROW, 18), end_datetime=at(TOMORROW, 19))

        response = self.book()

        self.assert_error(response, status.HTTP_409_CONFLICT, "overlapping active reservation")

    def test_concurrent_booking_race_returns_409(self):
        """Both requests pass validation; the DB ExclusionConstraint decides."""
        create_reservation(court=self.court, start_datetime=at(TOMORROW, 18), end_datetime=at(TOMORROW, 19))

        with mock.patch(
            "apps.bookings.services.validate_slot",
            return_value=SlotValidationResult(is_valid=True, errors=[]),
        ):
            response = self.book()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Reservation.objects.count(), 1)

class SlotValidationTests(ReservationAPITestCase):
    def test_past_slot(self):
        response = self.book(date=TODAY.isoformat(), start_time="08:00", end_time="09:00")

        self.assert_error(response, status.HTTP_400_BAD_REQUEST, "in the past")

    def test_failed_booking_creates_nothing(self):
        self.book(start_time="18:30", end_time="19:30")

        self.assertFalse(Reservation.objects.exists())


class CourtAccessTests(ReservationAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.private_club = create_members_only_club(name="Private")
        cls.private_court = create_court(club=cls.private_club)
        add_full_week_slots(cls.private_court, open_time=time(8), close_time=time(22))

    def test_non_member_cannot_book_private_court_and_learns_nothing(self):
        private = self.book(court=str(self.private_court.id))
        unknown = self.book(court="00000000-0000-0000-0000-000000000000")

        self.assertEqual(private.status_code, status.HTTP_400_BAD_REQUEST)
        # Indistinguishable from a non-existent id (apart from echoing the input back).
        private_error, unknown_error = private.data["court"][0], unknown.data["court"][0]
        self.assertEqual(private_error.code, "does_not_exist")
        self.assertEqual(unknown_error.code, "does_not_exist")
        self.assertEqual(
            private_error.replace(str(self.private_court.id), "<id>"),
            unknown_error.replace("00000000-0000-0000-0000-000000000000", "<id>"),
        )
        self.assertFalse(Reservation.objects.exists())

    def test_member_can_book_private_court(self):
        create_club_membership(user=self.player, club=self.private_club)

        self.assertEqual(self.book(court=str(self.private_court.id)).status_code, status.HTTP_201_CREATED)

    def test_inactive_court_cannot_be_booked(self):
        self.court.is_active = False
        self.court.save(update_fields=["is_active"])

        self.assertIn("court", self.book().data)

class RequestValidationTests(ReservationAPITestCase):
    def assert_field_error(self, field: str, **overrides):
        response = self.book(**overrides)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn(field, response.data)

    def test_required_fields(self):
        response = self.client.post(URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(set(response.data), {"court", "date", "start_time", "end_time"})

    def test_end_before_start(self):
        self.assert_field_error("end_time", start_time="19:00", end_time="18:00")

    def test_malformed_values(self):
        self.assert_field_error("date", date="08/01/2030")
        self.assert_field_error("start_time", start_time="25:00")
        self.assert_field_error("court", court="not-a-uuid")

    def test_staff_only_booking_types_are_rejected(self):
        for booking_type in ("BLOCKED", "TOURNAMENT"):
            with self.subTest(booking_type=booking_type):
                self.assert_field_error("booking_type", booking_type=booking_type)

    def test_unknown_guest(self):
        response = self.book(guests=["nobody@example.com"])

        self.assert_error(response, status.HTTP_400_BAD_REQUEST, "nobody@example.com", field="guests")

    def test_self_as_guest(self):
        self.assert_error(
            self.book(guests=["player@example.com"]),
            status.HTTP_400_BAD_REQUEST, "yourself", field="guests",
        )

    def test_too_many_guests(self):
        guests = [create_user().email for _ in range(4)]

        self.assert_field_error("guests", guests=guests)

    def test_guest_emails_are_case_insensitive_and_deduplicated(self):
        response = self.book(guests=["FRIEND@example.com", "friend@example.com"])

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["guests"], ["friend@example.com"])
