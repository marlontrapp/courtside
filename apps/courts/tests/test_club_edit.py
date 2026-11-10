"""Tests for club editing: GET/PATCH /api/v1/clubs/<uuid>/."""

from django.urls import reverse
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import create_club, create_superuser, create_user
from apps.courts.models import Club


def url(club) -> str:
    return reverse("courts:club-detail", args=[club.pk])


class ClubDetailGetTests(APITestCase):
    def setUp(self):
        self.club = create_club(name="Ace Club", address="Rua X, 10")

    def test_any_authenticated_user_can_view_a_club(self):
        self.client.force_authenticate(create_user())

        response = self.client.get(url(self.club))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.data),
            {"id", "name", "slug", "address", "timezone", "access_type",
             "cancellation_window_hours", "is_active", "created_at", "updated_at"},
        )
        self.assertEqual(response.data["name"], "Ace Club")

    def test_requires_authentication(self):
        response = self.client.get(url(self.club))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_club_is_404(self):
        self.client.force_authenticate(create_user())

        response = self.client.get(reverse("courts:club-detail", args=["11111111-1111-1111-1111-111111111111"]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ClubEditAPITests(APITestCase):
    def setUp(self):
        self.club = create_club(
            name="Ace Club",
            address="Rua X, 10",
            timezone="America/Sao_Paulo",
            access_type=Club.AccessType.OPEN,
            cancellation_window_hours=24,
            is_active=True,
        )
        self.staff = create_superuser()
        self.client.force_authenticate(self.staff)

    def patch(self, data):
        return self.client.patch(url(self.club), data, format="json")

    def test_staff_can_edit_every_setting_but_name_and_address(self):
        response = self.patch({
            "timezone": "Europe/Lisbon",
            "access_type": "MEMBERS_ONLY",
            "cancellation_window_hours": 6,
            "is_active": False,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.club.refresh_from_db()
        self.assertEqual(self.club.timezone, "Europe/Lisbon")
        self.assertEqual(self.club.access_type, Club.AccessType.MEMBERS_ONLY)
        self.assertEqual(self.club.cancellation_window_hours, 6)
        self.assertFalse(self.club.is_active)
        # name/address/slug untouched
        self.assertEqual((self.club.name, self.club.address, self.club.slug),
                          ("Ace Club", "Rua X, 10", "ace-club"))

    def test_partial_update_leaves_other_fields_untouched(self):
        response = self.patch({"is_active": False})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.club.refresh_from_db()
        self.assertFalse(self.club.is_active)
        self.assertEqual(self.club.timezone, "America/Sao_Paulo")
        self.assertEqual(self.club.access_type, Club.AccessType.OPEN)
        self.assertEqual(self.club.cancellation_window_hours, 24)

    def test_response_is_the_full_club_representation(self):
        response = self.patch({"cancellation_window_hours": 1})

        self.club.refresh_from_db()
        data = dict(response.data)
        created_at, updated_at = parse_datetime(data.pop("created_at")), parse_datetime(data.pop("updated_at"))
        self.assertEqual(
            data,
            {
                "id": str(self.club.id),
                "name": "Ace Club",
                "slug": "ace-club",
                "address": "Rua X, 10",
                "timezone": "America/Sao_Paulo",
                "access_type": "OPEN",
                "cancellation_window_hours": 1,
                "is_active": True,
            },
        )
        self.assertEqual(created_at, self.club.created_at)
        self.assertEqual(updated_at, self.club.updated_at)

    def test_name_is_rejected_even_when_unchanged(self):
        response = self.patch({"name": "Ace Club"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("contact support", response.data["name"][0])
        self.club.refresh_from_db()
        self.assertEqual(self.club.name, "Ace Club")

    def test_name_and_address_rejected_alongside_valid_fields(self):
        response = self.patch({"name": "New Name", "address": "Somewhere else", "is_active": False})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(set(response.data), {"name", "address"})
        self.assertIn("contact support", response.data["address"][0])
        self.club.refresh_from_db()
        self.assertEqual((self.club.name, self.club.address, self.club.is_active), ("Ace Club", "Rua X, 10", True))

    def test_invalid_values_are_rejected(self):
        for field, value in [
            ("timezone", "Mars/Olympus_Mons"),  # not an IANA zone
            ("access_type", "VIP_ONLY"),
            ("cancellation_window_hours", -1),
        ]:
            with self.subTest(field):
                response = self.patch({field: value})

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)
        self.club.refresh_from_db()
        self.assertEqual(self.club.timezone, "America/Sao_Paulo")

    def test_slug_in_body_is_ignored(self):
        response = self.patch({"slug": "hijacked", "is_active": False})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.club.refresh_from_db()
        self.assertEqual(self.club.slug, "ace-club")

    def test_regular_user_cannot_edit(self):
        self.client.force_authenticate(create_user())

        response = self.patch({"is_active": False})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.club.refresh_from_db()
        self.assertTrue(self.club.is_active)

    def test_anonymous_cannot_edit(self):
        self.client.force_authenticate(None)

        response = self.patch({"is_active": False})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_put_and_delete_are_not_allowed(self):
        for method in ("put", "delete"):
            with self.subTest(method):
                response = getattr(self.client, method)(url(self.club), {"is_active": False}, format="json")

                self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
