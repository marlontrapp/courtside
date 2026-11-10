"""Tests for club registration: POST /api/v1/clubs/ and the register_club service."""

from unittest import mock

from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import create_club, create_user
from apps.courts.models import Club
from apps.courts.services import register_club, unique_club_slug

URL = reverse("courts:club-register")


class ClubRegistrationAPITests(APITestCase):
    def setUp(self):
        self.user = create_user()
        self.client.force_authenticate(self.user)

    def post(self, data):
        return self.client.post(URL, data, format="json")

    def test_registers_club_with_name_and_address(self):
        response = self.post({"name": "Ibirapuera Tennis Center", "address": "Av. Pedro Álvares Cabral, SP"})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        club = Club.objects.get()
        self.assertEqual(club.name, "Ibirapuera Tennis Center")
        self.assertEqual(club.address, "Av. Pedro Álvares Cabral, SP")
        self.assertEqual(club.slug, "ibirapuera-tennis-center")
        self.assertEqual(
            response.data,
            {
                "id": str(club.id),
                "name": "Ibirapuera Tennis Center",
                "slug": "ibirapuera-tennis-center",
                "address": "Av. Pedro Álvares Cabral, SP",
                "timezone": "America/Sao_Paulo",
                "access_type": "OPEN",
                "cancellation_window_hours": 24,
                "is_active": True,
                "created_at": response.data["created_at"],
                "updated_at": response.data["updated_at"],
            },
        )

    def test_other_settings_start_at_defaults_and_are_not_settable(self):
        response = self.post({
            "name": "Jardins",
            "address": "Rua Augusta, SP",
            "slug": "custom",
            "timezone": "Europe/Lisbon",
            "access_type": "MEMBERS_ONLY",
            "cancellation_window_hours": 1,
            "is_active": False,
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        club = Club.objects.get()
        self.assertEqual(club.slug, "jardins")
        self.assertEqual(club.timezone, "America/Sao_Paulo")
        self.assertEqual(club.access_type, Club.AccessType.OPEN)
        self.assertEqual(club.cancellation_window_hours, 24)
        self.assertTrue(club.is_active)

    def test_strips_surrounding_whitespace(self):
        response = self.post({"name": "  Morumbi  ", "address": "  Rua X, 10  "})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual((response.data["name"], response.data["address"]), ("Morumbi", "Rua X, 10"))

    def test_name_and_address_are_required(self):
        response = self.post({})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(set(response.data), {"name", "address"})
        self.assertFalse(Club.objects.exists())

    def test_blank_name_or_address_is_rejected(self):
        for data, field in [
            ({"name": "", "address": "Rua X"}, "name"),
            ({"name": "   ", "address": "Rua X"}, "name"),
            ({"name": "Club", "address": ""}, "address"),
            ({"name": "Club", "address": "  \n "}, "address"),
            ({"name": None, "address": "Rua X"}, "name"),
        ]:
            with self.subTest(data=data):
                response = self.post(data)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(set(response.data), {field})
        self.assertFalse(Club.objects.exists())

    def test_name_longer_than_column_is_rejected(self):
        response = self.post({"name": "x" * 256, "address": "Rua X"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)

    def test_duplicate_names_fall_back_to_address_then_numbers(self):
        slugs = [
            self.post({"name": "Tennis Club", "address": address}).data["slug"]
            for address in ["Rua Augusta, 100", "Rua Augusta, 100", "Av. Paulista", "Rua Augusta, 100"]
        ]

        self.assertEqual(
            slugs,
            [
                "tennis-club",                      # name is free
                "tennis-club-rua-augusta-100",      # name taken -> name + address
                "tennis-club-av-paulista",          # a different address is free
                "tennis-club-rua-augusta-100-2",    # same name + address -> number
            ],
        )
        self.assertEqual(Club.objects.filter(name="Tennis Club").count(), 4)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)

        response = self.post({"name": "Club", "address": "Rua X"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Club.objects.exists())

class UniqueClubSlugTests(TestCase):
    def test_takes_next_free_number(self):
        create_club(name="Ace Club")
        for slug in ["ace-club-rua-x-10", "ace-club-rua-x-10-2", "ace-club-rua-x-10-3"]:
            create_club(name="Ace Club", slug=slug)

        self.assertEqual(unique_club_slug("Ace Club", "Rua X, 10"), "ace-club-rua-x-10-4")

    def test_similar_slugs_do_not_count_as_taken(self):
        create_club(name="Ace Club Lisboa")
        create_club(name="Ace Club", slug="ace-club-rua-x-100")

        self.assertEqual(unique_club_slug("Ace Club", "Rua X, 10"), "ace-club")

    def test_slug_taken_by_another_club_name_counts_as_taken(self):
        create_club(name="Ace Club")
        create_club(name="Ace Club Rua X 10")  # slug "ace-club-rua-x-10"

        self.assertEqual(unique_club_slug("Ace Club", "Rua X, 10"), "ace-club-rua-x-10-2")

    def test_uses_a_single_query(self):
        create_club(name="Ace Club")
        create_club(name="Ace Club", slug="ace-club-rua-x-10")

        with self.assertNumQueries(1):
            unique_club_slug("Ace Club", "Rua X, 10")

    def test_address_without_slug_characters_goes_to_numbers(self):
        create_club(name="Ace Club")

        self.assertEqual(unique_club_slug("Ace Club", "!!!"), "ace-club-2")

    def test_name_without_slug_characters_uses_address(self):
        create_club(name="Club")  # slug "club", the fallback

        self.assertEqual(unique_club_slug("東京テニス", "Rua X, 10"), "rua-x-10")
        self.assertEqual(unique_club_slug("東京テニス", "東京"), "club-2")

    def test_long_values_fit_the_column(self):
        max_length = Club._meta.get_field("slug").max_length
        name, address = "a" * 255, "b" * 1000
        for _ in range(3):
            slug = unique_club_slug(name, address)
            self.assertLessEqual(len(slug), max_length)
            create_club(name=name, slug=slug)

        self.assertEqual(
            list(Club.objects.order_by("created_at").values_list("slug", flat=True)),
            ["a" * 255, "a" * 247, "a" * 247 + "-2"],
        )


class RegisterClubServiceTests(TestCase):
    def test_retries_when_slug_is_taken_concurrently(self):
        create_club(name="Ace")  # simulates a concurrent insert after the slug check

        with mock.patch(
            "apps.courts.services.unique_club_slug", side_effect=["ace", "ace-2"]
        ) as slug_for:
            club = register_club(name="Ace", address="Rua X")

        self.assertEqual(slug_for.call_count, 2)
        self.assertEqual(club.slug, "ace-2")
        # The failed insert was rolled back to a savepoint; the transaction is usable.
        self.assertEqual(Club.objects.filter(name="Ace").count(), 2)

    def test_gives_up_after_repeated_collisions(self):
        create_club(name="Ace")

        with mock.patch("apps.courts.services.unique_club_slug", return_value="ace"):
            with self.assertRaises(IntegrityError):
                register_club(name="Ace", address="Rua X")

        self.assertEqual(Club.objects.count(), 1)
