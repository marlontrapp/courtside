"""API tests for GET /api/v1/me/ — the signed-in user and how they can act."""

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import (
    create_club,
    create_club_staff_role,
    create_player_profile,
    create_superuser,
    create_user,
)
from apps.courts.models import ClubStaffRole

URL = reverse("users:me")
Role = ClubStaffRole.Role


class MeAPITests(APITestCase):
    def get(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_identity_and_shape(self):
        user = create_user(email="ana@example.com", first_name="Ana", last_name="Silva")

        data = self.get(user)

        self.assertEqual(
            set(data),
            {"id", "email", "first_name", "last_name", "is_player", "is_site_staff", "clubs"},
        )
        self.assertEqual(data["id"], str(user.pk))
        self.assertEqual(data["email"], "ana@example.com")
        self.assertEqual((data["first_name"], data["last_name"]), ("Ana", "Silva"))
        self.assertFalse(data["is_site_staff"])
        self.assertEqual(data["clubs"], [])

    def test_player_means_having_a_player_profile(self):
        player = create_user()
        create_player_profile(user=player)

        self.assertTrue(self.get(player)["is_player"])
        self.assertFalse(self.get(create_user())["is_player"])

    def test_lists_clubs_with_current_roles_by_name(self):
        user = create_user()
        zulu = create_club(name="Zulu Tennis", address="Rua Z, 1")
        alpha = create_club(name="Alpha Club", address="Av. A, 2")
        create_club_staff_role(user=user, club=zulu, role=Role.ADMIN)
        create_club_staff_role(user=user, club=alpha, role=Role.FRONT_DESK)
        create_club_staff_role(user=user, club=alpha, role=Role.ADMIN)
        create_club_staff_role(user=user, club=alpha, role=Role.GROUNDSKEEPER)
        create_club_staff_role(user=create_user(), club=create_club(name="Other"), role=Role.ADMIN)

        clubs = self.get(user)["clubs"]

        self.assertEqual([club["name"] for club in clubs], ["Alpha Club", "Zulu Tennis"])
        # Declaration order: admin, groundskeeper, front desk.
        self.assertEqual(clubs[0]["roles"], ["ADMIN", "GROUNDSKEEPER", "FRONT_DESK"])
        self.assertEqual(clubs[1]["roles"], ["ADMIN"])
        self.assertEqual(
            set(clubs[0]),
            {"id", "name", "slug", "address", "timezone", "access_type", "is_active", "roles"},
        )
        self.assertEqual(clubs[0]["address"], "Av. A, 2")

    def test_only_roles_in_effect_today_count(self):
        user = create_user()
        today = timezone.localdate()
        create_club_staff_role(
            user=user, club=create_club(name="Ended"), role=Role.ADMIN,
            start_date=today - timedelta(days=30), end_date=today - timedelta(days=1),
        )
        create_club_staff_role(
            user=user, club=create_club(name="Future"), role=Role.ADMIN,
            start_date=today + timedelta(days=1),
        )
        create_club_staff_role(
            user=user, club=create_club(name="Deactivated"), role=Role.ADMIN, is_active=False,
        )
        create_club_staff_role(
            user=user, club=create_club(name="Last day"), role=Role.FRONT_DESK,
            start_date=today - timedelta(days=3), end_date=today,
        )

        clubs = self.get(user)["clubs"]

        self.assertEqual([(c["name"], c["roles"]) for c in clubs], [("Last day", ["FRONT_DESK"])])

    def test_site_staff_can_work_in_every_club(self):
        staff = create_superuser()
        mine = create_club(name="Beta")
        create_club_staff_role(user=staff, club=mine, role=Role.ADMIN)
        create_club(name="Alpha", is_active=False)

        data = self.get(staff)

        self.assertTrue(data["is_site_staff"])
        self.assertEqual(
            [(c["name"], c["roles"], c["is_active"]) for c in data["clubs"]],
            [("Alpha", [], False), ("Beta", ["ADMIN"], True)],
        )

    def test_query_count_does_not_grow_with_clubs(self):
        user = create_user()
        create_player_profile(user=user)

        def query_count() -> int:
            self.client.force_authenticate(user)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(URL).status_code, 200)
            return len(ctx.captured_queries)

        create_club_staff_role(user=user, club=create_club(), role=Role.ADMIN)
        baseline = query_count()
        for _ in range(5):
            create_club_staff_role(user=user, club=create_club(), role=Role.FRONT_DESK)

        self.assertEqual(query_count(), baseline)
