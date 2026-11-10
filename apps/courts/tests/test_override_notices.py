"""
One-time overrides as the way to say why a court isn't bookable (tournament,
maintenance, weather, …): search results, notices, booking errors, API.

Time is frozen on Monday 2030-01-07 12:00 in São Paulo.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import (
    add_full_week_slots,
    create_club,
    create_club_staff_role,
    create_court,
    create_day_template,
    create_schedule_override,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import ClubStaffRole, ScheduleOverride

SP = ZoneInfo("America/Sao_Paulo")
TOURNAMENT_DAY = date(2030, 1, 11)  # Friday


class OverrideSearchTests(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 12, tzinfo=SP)

    def setUp(self):
        super().setUp()
        club = create_club()
        self.court_1 = create_court(club=club, name="Court 1")
        self.court_2 = create_court(club=club, name="Court 2")
        for court in (self.court_1, self.court_2):
            add_full_week_slots(court, open_time=time(7), close_time=time(22))
        # Court 1: tournament until 14:00 — only the afternoon slots that day.
        afternoon = create_day_template(club, [(time(h), time(h + 1)) for h in range(14, 22)], "Afternoon only")
        create_schedule_override(
            self.court_1, TOURNAMENT_DAY, afternoon,
            type=ScheduleOverride.Type.TOURNAMENT, notes="Junior club championship until 14:00",
        )
        self.client.force_authenticate(create_user())

    def search(self, **params):
        response = self.client.get(reverse("courts:court-list"), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {court["name"]: court for court in response.json()["results"]}

    def test_a_time_with_no_bookable_slot_is_not_returned(self):
        results = self.search(date="2030-01-11", time_from="09:00", time_to="12:00")

        self.assertEqual(list(results), ["Court 2"])

    def test_returned_when_the_window_includes_bookable_slots_with_the_notice(self):
        results = self.search(date="2030-01-11")

        self.assertEqual(results["Court 1"]["available_slots"][0]["start"], "2030-01-11T14:00:00-03:00")
        self.assertEqual(results["Court 1"]["notices"], [{
            "date": "2030-01-11", "type": "TOURNAMENT", "notes": "Junior club championship until 14:00",
        }])
        self.assertEqual(results["Court 2"]["notices"], [])

    def test_a_closed_day_is_not_returned_for_that_day(self):
        create_schedule_override(self.court_2, TOURNAMENT_DAY, None, type="WEATHER", notes="Flooded")

        self.assertEqual(list(self.search(date="2030-01-11", time_from="09:00", time_to="12:00")), [])

    def test_closed_day_notice_shows_in_a_wider_search(self):
        create_schedule_override(self.court_2, TOURNAMENT_DAY, None, type="WEATHER", notes="Flooded")

        results = self.search(date_from="2030-01-10", date_to="2030-01-11")

        self.assertEqual(results["Court 2"]["notices"], [{"date": "2030-01-11", "type": "WEATHER", "notes": "Flooded"}])
        self.assertTrue(all(s["start"].startswith("2030-01-10") for s in results["Court 2"]["available_slots"]))

class OverrideTypeNotesAPITests(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 12, tzinfo=SP)

    def setUp(self):
        super().setUp()
        self.club = create_club()
        self.court = create_court(club=self.club)
        admin = create_user()
        create_club_staff_role(user=admin, club=self.club, role=ClubStaffRole.Role.ADMIN, start_date=date(2030, 1, 1))
        self.client.force_authenticate(admin)
        self.url = reverse("courts:schedule-override-detail", args=[self.court.pk, "2030-01-11"])

    def test_type_is_required_and_must_be_known(self):
        for name, body in {"missing": {"day_template": None}, "unknown": {"day_template": None, "type": "PARTY"}}.items():
            with self.subTest(name):
                response = self.client.put(self.url, body, format="json")

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("type", response.data)

    def test_notes_are_optional(self):
        response = self.client.put(self.url, {"day_template": None, "type": "WEATHER"}, format="json")

        self.assertEqual(response.json()["notes"], "")
