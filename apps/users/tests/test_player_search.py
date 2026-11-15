"""Tests for player search: GET /api/v1/players/search/ and apps/users/search.py."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import create_player_profile, create_user
from apps.users.models import PlayerProfile, User
from apps.users.search import search_players
from apps.users.serializers import PlayerSearchResultSerializer

URL = reverse("users:player-search")


def player(first_name, last_name, location="", **profile_fields) -> PlayerProfile:
    user = create_user(first_name=first_name, last_name=last_name)
    return create_player_profile(user=user, location=location, **profile_fields)


def names(profiles) -> list[str]:
    return [f"{p.user.first_name} {p.user.last_name}" for p in profiles]


class PlayerSearchTestData:
    @classmethod
    def setUpTestData(cls):
        cls.ana = player("Ana", "Souza", "Pinheiros, São Paulo - SP, Brasil")
        cls.joao = player("João", "Gonçalves", "Alfama, Lisboa, Portugal")
        cls.bruno = player("Bruno", "Lima", "Jardins, São Paulo - SP, Brasil")
        cls.no_location = player("Carla", "Mendes")


class PlayerSearchMatchingTests(PlayerSearchTestData, TestCase):
    def test_by_first_last_or_full_name_whole_or_partial(self):
        for query in ("Ana", "Souza", "Ana Souza", "souz", "Souza Ana"):
            with self.subTest(query):
                self.assertEqual(names(search_players(query)), ["Ana Souza"])

    def test_by_location(self):
        self.assertEqual(names(search_players("Lisboa")), ["João Gonçalves"])
        self.assertEqual(names(search_players("Portugal")), ["João Gonçalves"])
        self.assertEqual(sorted(names(search_players("são paulo"))), ["Ana Souza", "Bruno Lima"])
        self.assertEqual(names(search_players("pinhei")), ["Ana Souza"])

    def test_words_can_match_name_and_location_together(self):
        self.assertEqual(names(search_players("bruno são paulo")), ["Bruno Lima"])
        self.assertEqual(names(search_players("ana lisboa")), [])  # every word must match

    def test_ignores_case_and_accents_both_ways(self):
        for query in ("JOAO GONCALVES", "joão gonçalves", "Joao", "alfama LISBOA"):
            with self.subTest(query):
                self.assertEqual(names(search_players(query)), ["João Gonçalves"])
        self.assertEqual(names(search_players("Ánã")), ["Ana Souza"])

    def test_players_without_location_are_found_by_name(self):
        self.assertEqual(names(search_players("carla")), ["Carla Mendes"])

    def test_only_active_users_with_a_player_profile(self):
        create_user(first_name="Ana", last_name="Staffonly")  # no profile: not a player
        inactive = player("Ana", "Inactive", "São Paulo")
        User.objects.filter(pk=inactive.user_id).update(is_active=False)

        self.assertEqual(names(search_players("ana")), ["Ana Souza"])

    def test_email_and_phone_are_not_searchable(self):
        user = create_user(email="secret.handle@example.com", first_name="Zed", last_name="Hidden")
        create_player_profile(user=user, phone_number="+55 11 98888-7777")

        self.assertEqual(list(search_players("secret.handle")), [])
        self.assertEqual(list(search_players("example.com")), [])
        self.assertEqual(list(search_players("98888")), [])
        self.assertEqual(names(search_players("zed")), ["Zed Hidden"])


class SearchColumnsMaintenanceTests(TestCase):
    """The search columns span users_user and users_playerprofile: triggers keep them current."""

    def document(self, profile) -> str:
        return PlayerProfile.objects.values_list("search_document", flat=True).get(pk=profile.pk)

    def test_new_profile(self):
        profile = player("Ána", "Sôuza", "Pinheiros, São Paulo")

        self.assertEqual(self.document(profile), "ana souza pinheiros, sao paulo")
        self.assertEqual(PlayerProfile.objects.get(pk=profile.pk).search_name, "ana souza")

    def test_renaming_the_user(self):
        profile = player("Ana", "Souza", "Lisboa")
        user = profile.user

        user.first_name, user.last_name = "Anabela", "Gonçalves"
        user.save()
        self.assertEqual(names(search_players("gonçalves")), ["Anabela Gonçalves"])
        self.assertEqual(list(search_players("souza")), [])

        User.objects.filter(pk=user.pk).update(last_name="")  # queryset updates too
        self.assertEqual(self.document(profile), "anabela lisboa")

    def test_changing_the_location(self):
        profile = player("Ana", "Souza", "Lisboa")

        profile.location = "Porto, Portugal"
        profile.save()
        self.assertEqual(names(search_players("porto")), ["Ana Souza"])
        self.assertEqual(list(search_players("lisboa")), [])

        PlayerProfile.objects.filter(pk=profile.pk).update(location="")
        self.assertEqual(self.document(profile), "ana souza")

    def test_saving_other_fields_keeps_the_columns(self):
        profile = player("Ana", "Souza", "Lisboa")

        profile.racket = "Head Prestige"
        profile.save()  # Django also writes the search columns ("" in Python); the trigger recomputes them
        PlayerProfile.objects.filter(pk=profile.pk).update(skill_level="4.0")

        self.assertEqual(self.document(profile), "ana souza lisboa")

    def test_direct_writes_to_the_search_columns_are_recomputed(self):
        profile = player("Ana", "Souza", "Lisboa")

        PlayerProfile.objects.filter(pk=profile.pk).update(search_document="tampered", search_name="x")

        self.assertEqual(self.document(profile), "ana souza lisboa")

    def test_the_real_columns_keep_their_accents(self):
        profile = player("João", "Gonçalves", "Niterói - RJ")

        stored = PlayerProfile.objects.select_related("user").get(pk=profile.pk)
        self.assertEqual((stored.user.first_name, stored.user.last_name, stored.location), ("João", "Gonçalves", "Niterói - RJ"))


class PlayerSearchRankingTests(TestCase):
    def test_best_matches_first(self):
        location_only = player("Zoe", "Alves", "Rua Costa Lima, São Paulo")
        all_words_in_name = player("Lima", "Costa")
        name_contains = player("Ana", "Costa Lima")
        name_starts = player("Costa", "Limão")

        self.assertEqual(
            list(search_players("costa lima")), [name_starts, name_contains, all_words_in_name, location_only]
        )

    def test_ties_by_name_ignoring_accents_and_case(self):
        for first in ("Élise", "bruno", "Ana"):
            player(first, "Tennisfan")

        self.assertEqual(names(search_players("tennisfan")), ["Ana Tennisfan", "bruno Tennisfan", "Élise Tennisfan"])


class PlayerSearchQueryTests(PlayerSearchTestData, TestCase):
    def test_uses_the_trigram_index_with_constant_patterns(self):
        # The test table is tiny, so the planner rightly prefers scanning it. Forbid
        # sequential and plain index scans (a full scan of another index is a scan
        # in disguise) to check the trigram index *can* serve the query — i.e. the
        # words were folded into constant LIKE patterns. GIN only does bitmap scans.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_indexscan = off")
        plan = search_players("João Lisboa").explain()

        self.assertIn("player_search_document_trgm", plan)
        self.assertIn("'%joao%'", plan)
        self.assertIn("'%lisboa%'", plan)

    def test_serializer_reads_only_loaded_fields(self):
        results = list(search_players("ana"))

        with self.assertNumQueries(0):  # a field outside RESULT_FIELDS would query per result
            PlayerSearchResultSerializer(results, many=True).data


class PlayerSearchAPITests(PlayerSearchTestData, APITestCase):
    def setUp(self):
        self.client.force_authenticate(create_user())

    def test_returns_public_details_only(self):
        PlayerProfile.objects.filter(pk=self.ana.pk).update(
            skill_level="4.5", phone_number="+55 11 90000-0000", gender="FEMALE", picture_key="players/a/p.jpg"
        )

        response = self.client.get(URL, {"q": "ana souza"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["next"], None)
        self.assertNotIn("count", response.data)
        self.assertEqual(
            response.data["results"],
            [{
                "id": str(self.ana.user_id),
                "first_name": "Ana",
                "last_name": "Souza",
                "location": "Pinheiros, São Paulo - SP, Brasil",
                "skill_level": "4.5",
                "picture_url": "http://testserver/media/players/a/p.jpg",
            }],
        )

    def test_no_picture_is_null(self):
        response = self.client.get(URL, {"q": "bruno"})

        self.assertIsNone(response.data["results"][0]["picture_url"])

    def test_one_query_per_request(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(URL, {"q": "são paulo"})

        self.assertEqual(len(response.data["results"]), 2)
        statements = [q["sql"] for q in queries if "SAVEPOINT" not in q["sql"]]
        self.assertEqual(len(statements), 1, statements)  # no COUNT(*), no per-result queries

    def test_pagination(self):
        for number in range(27):
            player(f"Pager{number:02}", "Player")

        first = self.client.get(URL, {"q": "pager"})
        second = self.client.get(first.data["next"])

        self.assertEqual(len(first.data["results"]), 25)
        self.assertEqual([r["first_name"] for r in second.data["results"]], ["Pager25", "Pager26"])
        self.assertIsNone(second.data["next"])

    def test_invalid_queries(self):
        for params, message in (
            ({}, "Enter a player name or location to search for."),
            ({"q": "  "}, "Enter a player name or location to search for."),
            ({"q": "jo"}, "Include at least one word with 3 or more letters or digits."),
        ):
            with self.subTest(params):
                response = self.client.get(URL, params)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data, {"q": [message]})

    def test_requires_login(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.client.get(URL, {"q": "ana"}).status_code, status.HTTP_403_FORBIDDEN)
