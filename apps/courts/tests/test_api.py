"""API tests for GET /api/v1/courts/ (listing + filters + availability)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

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
from apps.courts.models import Court

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)  # a Monday
TOMORROW = TODAY + timedelta(days=1)
URL = reverse("courts:court-list")


def at(day: date, hour: int, minute: int = 0) -> str:
    return datetime.combine(day, time(hour, minute), tzinfo=SAO_PAULO).isoformat()


class CourtListAPITestCase(FrozenTimeMixin, APITestCase):
    # 09:30 today: the 08:00 and 09:00 slots have already started.
    frozen_now = datetime.combine(TODAY, time(9, 30), tzinfo=SAO_PAULO)

    @classmethod
    def setUpTestData(cls):
        # Created once per class; each test runs in a rolled-back savepoint.
        cls.user = create_user()
        cls.club_a = create_club(name="Club A")
        cls.club_b = create_club(name="Club B")
        cls.clay = cls._court(cls.club_a, "A1 Clay", Court.SurfaceType.CLAY, indoor=False)
        cls.hard_indoor = cls._court(cls.club_a, "A2 Hard", Court.SurfaceType.HARD, indoor=True)
        cls.grass = cls._court(cls.club_b, "B1 Grass", Court.SurfaceType.GRASS, indoor=False)

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    @staticmethod
    def _court(club, name, surface, *, indoor):
        court = create_court(club=club, name=name, surface_type=surface, is_indoor=indoor)
        add_full_week_slots(court, open_time=time(8), close_time=time(12))
        return court

    def get(self, **params):
        return self.client.get(URL, params)

    def names(self, response) -> list[str]:
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [court["name"] for court in response.data["results"]]

    def slots(self, response, court_name: str) -> list[str]:
        court = next(c for c in response.data["results"] if c["name"] == court_name)
        return [slot["start"] for slot in court["available_slots"]]


class AuthTests(CourtListAPITestCase):
    def test_requires_authentication(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.get().status_code, status.HTTP_403_FORBIDDEN)


class ResponseShapeTests(CourtListAPITestCase):
    def test_court_payload(self):
        response = self.get(date=TOMORROW.isoformat(), club=str(self.club_b.id))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        court = response.data["results"][0]
        self.assertEqual(
            set(court),
            {
                "id", "name", "surface_type", "is_indoor", "has_lighting",
                "hourly_rate", "lighting_fee", "club", "available_slots", "notices",
            },
        )
        self.assertEqual(
            court["club"],
            {
                "id": str(self.club_b.id),
                "name": "Club B",
                "slug": "club-b",
                "timezone": "America/Sao_Paulo",
                "access_type": "OPEN",
            },
        )
        self.assertEqual(
            court["available_slots"][0],
            {"start": "2030-01-08T08:00:00-03:00", "end": "2030-01-08T09:00:00-03:00"},
        )


class AvailabilityWindowTests(CourtListAPITestCase):
    def test_no_params_returns_todays_remaining_slots(self):
        response = self.get()

        self.assertEqual(self.names(response), ["A1 Clay", "A2 Hard", "B1 Grass"])
        self.assertEqual(self.slots(response, "A1 Clay"), [at(TODAY, 10), at(TODAY, 11)])

    def test_no_params_keeps_courts_without_availability(self):
        create_reservation(
            court=self.clay, start_datetime=at(TODAY, 10), end_datetime=at(TODAY, 12)
        )

        response = self.get()

        self.assertIn("A1 Clay", self.names(response))
        self.assertEqual(self.slots(response, "A1 Clay"), [])

    def test_date_only_returns_all_slots_of_that_day(self):
        response = self.get(date=TOMORROW.isoformat())

        self.assertEqual(
            self.slots(response, "A1 Clay"),
            [at(TOMORROW, h) for h in (8, 9, 10, 11)],
        )

    def test_date_and_time_returns_only_slots_inside_the_window(self):
        response = self.get(date=TOMORROW.isoformat(), time_from="09:00", time_to="11:00")

        self.assertEqual(self.slots(response, "A1 Clay"), [at(TOMORROW, 9), at(TOMORROW, 10)])

    def test_time_without_date_applies_to_today(self):
        response = self.get(time_from="11:00")

        self.assertEqual(self.slots(response, "A1 Clay"), [at(TODAY, 11)])

    def test_booked_slots_are_excluded(self):
        create_reservation(
            court=self.clay, start_datetime=at(TOMORROW, 9), end_datetime=at(TOMORROW, 10)
        )

        response = self.get(date=TOMORROW.isoformat())

        self.assertNotIn(at(TOMORROW, 9), self.slots(response, "A1 Clay"))
        self.assertIn(at(TOMORROW, 9), self.slots(response, "A2 Hard"))

    def test_date_range(self):
        response = self.get(
            date_from=TOMORROW.isoformat(),
            date_to=(TOMORROW + timedelta(days=2)).isoformat(),
            time_from="11:00",
        )

        self.assertEqual(
            self.slots(response, "A1 Clay"),
            [at(TOMORROW + timedelta(days=d), 11) for d in range(3)],
        )

    def test_full_week_range_is_allowed(self):
        response = self.get(
            date_from=TODAY.isoformat(), date_to=(TODAY + timedelta(days=6)).isoformat()
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_explicit_window_omits_courts_without_free_slots(self):
        create_reservation(
            court=self.clay, start_datetime=at(TOMORROW, 9), end_datetime=at(TOMORROW, 11)
        )

        response = self.get(date=TOMORROW.isoformat(), time_from="09:00", time_to="11:00")

        self.assertEqual(self.names(response), ["A2 Hard", "B1 Grass"])
        self.assertEqual(response.data["count"], 2)

    def test_blank_params_are_ignored(self):
        response = self.get(date="", time_from="", surface_type="")

        self.assertEqual(self.names(response), ["A1 Clay", "A2 Hard", "B1 Grass"])


class CourtFilterTests(CourtListAPITestCase):
    def test_filter_by_multiple_surface_types(self):
        self.assertEqual(
            self.names(self.get(surface_type="CLAY,GRASS")), ["A1 Clay", "B1 Grass"]
        )

    def test_filter_indoor_and_outdoor(self):
        self.assertEqual(self.names(self.get(is_indoor="true")), ["A2 Hard"])
        self.assertEqual(self.names(self.get(is_indoor="false")), ["A1 Clay", "B1 Grass"])

    def test_filter_by_multiple_clubs(self):
        club_c = create_club(name="Club C")
        self._court(club_c, "C1", Court.SurfaceType.CLAY, indoor=False)

        response = self.get(club=f"{self.club_a.id},{club_c.id}")

        self.assertEqual(self.names(response), ["A1 Clay", "A2 Hard", "C1"])

    def test_filters_are_combined_with_and(self):
        response = self.get(
            club=str(self.club_a.id),
            surface_type="CLAY,HARD",
            is_indoor="false",
            date=TOMORROW.isoformat(),
            time_from="08:00",
            time_to="09:00",
        )

        self.assertEqual(self.names(response), ["A1 Clay"])
        self.assertEqual(self.slots(response, "A1 Clay"), [at(TOMORROW, 8)])

    def test_inactive_courts_are_not_listed(self):
        self.grass.is_active = False
        self.grass.save(update_fields=["is_active"])

        self.assertNotIn("B1 Grass", self.names(self.get()))


class MembershipVisibilityTests(CourtListAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.private_club = create_members_only_club(name="Private")
        cls._court(cls.private_club, "P1", Court.SurfaceType.CLAY, indoor=False)

    def test_non_member_does_not_see_members_only_courts(self):
        self.assertNotIn("P1", self.names(self.get()))

    def test_member_sees_members_only_courts(self):
        create_club_membership(user=self.user, club=self.private_club)

        self.assertIn("P1", self.names(self.get()))

    def test_filtering_by_inaccessible_club_returns_nothing(self):
        response = self.get(club=str(self.private_club.id))

        self.assertEqual(response.data["count"], 0)


class ValidationTests(CourtListAPITestCase):
    def assert_400(self, field: str, **params):
        response = self.get(**params)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn(field, response.data)

    def test_range_longer_than_a_week(self):
        self.assert_400(
            "date_to",
            date_from=TODAY.isoformat(),
            date_to=(TODAY + timedelta(days=7)).isoformat(),
        )

    def test_date_and_range_are_mutually_exclusive(self):
        self.assert_400(
            "non_field_errors",
            date=TODAY.isoformat(),
            date_from=TODAY.isoformat(),
            date_to=TODAY.isoformat(),
        )

    def test_range_requires_both_ends(self):
        self.assert_400("date_to", date_from=TODAY.isoformat())
        self.assert_400("date_from", date_to=TODAY.isoformat())

    def test_reversed_range(self):
        self.assert_400("date_to", date_from=TOMORROW.isoformat(), date_to=TODAY.isoformat())

    def test_past_dates(self):
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        self.assert_400("date", date=yesterday)
        self.assert_400("date_from", date_from=yesterday, date_to=TODAY.isoformat())

    def test_reversed_time_window(self):
        self.assert_400("time_to", time_from="10:00", time_to="09:00")

    def test_malformed_values(self):
        self.assert_400("date", date="07/01/2030")
        self.assert_400("time_from", time_from="25:00")
        self.assert_400("surface_type", surface_type="MUD")
        self.assert_400("club", club="not-a-uuid")


class PerformanceTests(CourtListAPITestCase):
    def test_query_count_does_not_grow_with_courts(self):
        def query_count() -> int:
            from django.db import connection
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.get(date=TOMORROW.isoformat()).status_code, 200)
            return len(ctx.captured_queries)

        baseline = query_count()
        for i in range(10):
            self._court(self.club_b, f"Extra {i}", Court.SurfaceType.CLAY, indoor=False)

        self.assertEqual(query_count(), baseline)
