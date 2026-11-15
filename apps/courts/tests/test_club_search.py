"""Tests for club search: GET /api/v1/clubs/search/ and apps/courts/search.py."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import create_club, create_members_only_club, create_user
from apps.courts.models import Club
from apps.courts.search import search_clubs
from apps.courts.services import approve_club

URL = reverse("courts:club-search")


def names(clubs) -> list[str]:
    return [club.name for club in clubs]


class ClubSearchTestData:
    @classmethod
    def setUpTestData(cls):
        cls.ibirapuera = create_club(
            name="Ibirapuera Tennis Center", address="Av. Pedro Álvares Cabral, 1500, São Paulo - SP, Brasil"
        )
        cls.jardins = create_members_only_club(
            name="Jardins Country Club", address="Rua Estados Unidos, 100, São Paulo - SP, Brasil"
        )
        cls.lisboa = create_club(name="Lisboa Tennis Club", address="Av. da Liberdade, 20, Lisboa, Portugal")
        cls.porto = create_club(name="Clube de Ténis do Porto", address="Rua das Flores, 7, Porto, Portugal")
        cls.inactive = create_club(
            name="Old Ibirapuera Courts", address="Rua Velha, 1, São Paulo - SP, Brasil", is_active=False
        )


class SearchMatchingTests(ClubSearchTestData, TestCase):
    def test_by_name_whole_or_partial_word(self):
        for query in ("Ibirapuera", "ibira", "rapue", "Tennis Center"):
            with self.subTest(query):
                self.assertEqual(names(search_clubs(query)), ["Ibirapuera Tennis Center"])

    def test_by_any_part_of_the_address(self):
        cases = {
            "Pedro Álvares Cabral": ["Ibirapuera Tennis Center"],  # street
            "Estados Unidos 100": ["Jardins Country Club"],  # street + number
            "Liberdade": ["Lisboa Tennis Club"],
            "Porto": ["Clube de Ténis do Porto"],  # city (and name)
            "Portugal": ["Clube de Ténis do Porto", "Lisboa Tennis Club"],  # country
        }
        for query, expected in cases.items():
            with self.subTest(query):
                self.assertEqual(sorted(names(search_clubs(query))), sorted(expected))

    def test_words_can_match_name_and_address_together(self):
        self.assertEqual(names(search_clubs("tennis portugal")), ["Lisboa Tennis Club"])
        self.assertEqual(names(search_clubs("country são paulo")), ["Jardins Country Club"])

    def test_every_word_must_match(self):
        self.assertEqual(names(search_clubs("ibirapuera lisboa")), [])

    def test_ignores_case_and_accents_both_ways(self):
        for query in ("SAO PAULO", "são paulo", "Sâo Paùlo", "ALVARES", "álvares"):
            with self.subTest(query):
                self.assertIn("Ibirapuera Tennis Center", names(search_clubs(query)))
        # Accent in the data ("Ténis"), none in the query, and the other way round.
        self.assertEqual(names(search_clubs("tenis do porto")), ["Clube de Ténis do Porto"])
        self.assertEqual(names(search_clubs("Ténnis Center")), ["Ibirapuera Tennis Center"])

    def test_punctuation_in_the_query_is_ignored(self):
        for query in ("São Paulo - SP", "Av. Pedro Álvares Cabral, 1500", "(lisboa)", "rua-das-flores"):
            with self.subTest(query):
                self.assertTrue(search_clubs(query).exists(), query)

    def test_short_words_filter_when_combined_with_a_longer_one(self):
        self.assertEqual(
            sorted(names(search_clubs("paulo sp"))), ["Ibirapuera Tennis Center", "Jardins Country Club"]
        )
        self.assertEqual(names(search_clubs("paulo pt")), [])

    def test_like_wildcards_only_separate_words(self):
        # "%" and "_" never reach the LIKE pattern: like other punctuation, they split words.
        create_club(name="Club 733% Tennis", address="Rua Ab_Cd, 1")

        self.assertEqual(names(search_clubs("733%")), ["Club 733% Tennis"])
        self.assertEqual(names(search_clubs("%%733")), ["Club 733% Tennis"])
        self.assertEqual(names(search_clubs("ab_cd tennis")), ["Club 733% Tennis"])  # words: ab, cd, tennis
        self.assertEqual(names(search_clubs("ab_xy tennis")), [])  # "_" isn't "any character"

    def test_inactive_clubs_are_excluded_members_only_included(self):
        self.assertEqual(names(search_clubs("ibirapuera")), ["Ibirapuera Tennis Center"])
        self.assertEqual(names(search_clubs("jardins")), ["Jardins Country Club"])

    def test_search_follows_name_and_address_changes(self):
        # search_document is a generated column: the database keeps it current.
        Club.objects.filter(pk=self.lisboa.pk).update(address="Rua Nova, 5, Coimbra, Portugal")
        self.assertEqual(names(search_clubs("coimbra")), ["Lisboa Tennis Club"])
        self.assertEqual(names(search_clubs("liberdade")), [])

        self.porto.name = "Porto Racquet Club"
        self.porto.save()
        self.assertEqual(names(search_clubs("racquet")), ["Porto Racquet Club"])

        Club.objects.filter(pk=self.inactive.pk).update(is_active=True)
        self.assertEqual(len(search_clubs("ibirapuera")), 2)


class SearchRankingTests(TestCase):
    def test_best_matches_first(self):
        address_only = create_club(name="Alpha Club", address="Rua Tennis Center, 1")
        all_words_in_name = create_club(name="Center of Tennis", address="Rua X, 1")
        name_contains = create_club(name="Beta Tennis Center", address="Rua Y, 1")
        name_starts = create_club(name="Tennis Center Zeta", address="Rua Z, 1")

        self.assertEqual(
            list(search_clubs("tennis center")), [name_starts, name_contains, all_words_in_name, address_only]
        )

    def test_ties_are_ordered_by_name(self):
        for name in ("Gamma Tennis", "Alpha Tennis", "Beta Tennis"):
            create_club(name=name, address="Rua 1")

        self.assertEqual(names(search_clubs("tennis")), ["Alpha Tennis", "Beta Tennis", "Gamma Tennis"])


class SearchQueryPlanTests(ClubSearchTestData, TestCase):
    def test_uses_the_trigram_index_with_constant_patterns(self):
        # The test table is tiny, so the planner rightly prefers scanning it. Forbid
        # sequential and plain index scans (a full scan of another index is a scan
        # in disguise) to check the trigram index *can* serve the query — i.e. the
        # words were folded into constant LIKE patterns. GIN only does bitmap scans.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_indexscan = off")
        plan = search_clubs("Ibirapuera São").explain()

        self.assertIn("club_search_document_trgm", plan)
        self.assertIn("'%ibirapuera%'", plan)
        self.assertIn("'%sao%'", plan)

    def test_orders_by_rank_not_a_bare_limit(self):
        # A bare LIMIT lets the planner bet on a sequential scan; ordering by the
        # rank keeps the index plan (see README).
        sql = str(search_clubs("tennis").query)

        self.assertIn('ORDER BY', sql)
        self.assertIn('"courts_club"."name" ASC', sql)


class ClubSearchAPITests(ClubSearchTestData, APITestCase):
    def setUp(self):
        self.client.force_authenticate(create_user())

    def test_returns_matching_clubs(self):
        response = self.client.get(URL, {"q": "são paulo"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["next"], None)
        self.assertEqual(response.data["previous"], None)
        self.assertNotIn("count", response.data)
        self.assertEqual(
            response.data["results"],
            [
                {
                    "id": str(club.id),
                    "name": club.name,
                    "slug": club.slug,
                    "address": club.address,
                    "timezone": club.timezone,
                    "access_type": club.access_type,
                }
                for club in (self.ibirapuera, self.jardins)
            ],
        )

    def test_clubs_keep_their_accents_only_the_search_ignores_them(self):
        name, address = "Clube de Tênis Ipê", "Praça São João, 12, Florianópolis - SC, Brasil"
        registered = self.client.post(reverse("courts:club-register"), {"name": name, "address": address}, format="json")
        self.assertEqual(registered.status_code, status.HTTP_201_CREATED, registered.data)
        club_id = registered.data["id"]
        self.assertEqual(self.client.get(URL, {"q": "tenis ipe"}).data["results"], [])  # hidden until approved
        approve_club(Club.objects.get(pk=club_id), by=create_user(is_staff=True))

        found = self.client.get(URL, {"q": "tenis ipe sao joao florianopolis"})  # typed without accents

        self.assertEqual([(r["id"], r["name"], r["address"]) for r in found.data["results"]], [(club_id, name, address)])
        detail = self.client.get(reverse("courts:club-detail", args=[club_id]))
        self.assertEqual((detail.data["name"], detail.data["address"]), (name, address))
        stored = Club.objects.values("name", "address", "search_document").get(pk=club_id)
        self.assertEqual((stored["name"], stored["address"]), (name, address))  # the real columns
        self.assertEqual(  # only the search copy is unaccented
            stored["search_document"], "clube de tenis ipe praca sao joao, 12, florianopolis - sc, brasil"
        )

    def test_no_match_is_an_empty_list(self):
        response = self.client.get(URL, {"q": "atlantis"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"next": None, "previous": None, "results": []})

    def test_one_query_per_request(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(URL, {"q": "club"})

        self.assertEqual(len(response.data["results"]), 3)
        # ATOMIC_REQUESTS adds SAVEPOINT/RELEASE around the request in tests.
        statements = [q["sql"] for q in queries if "SAVEPOINT" not in q["sql"]]
        self.assertEqual(len(statements), 1, statements)  # no COUNT(*), no per-result queries

    def test_pagination_without_count(self):
        for number in range(30):
            create_club(name=f"Paging Club {number:02}", address="Rua P")

        first = self.client.get(URL, {"q": "paging"})
        self.assertEqual(len(first.data["results"]), 25)
        self.assertEqual(first.data["previous"], None)
        self.assertIn("page=2", first.data["next"])

        second = self.client.get(first.data["next"])
        self.assertEqual([r["name"] for r in second.data["results"]], [f"Paging Club {n}" for n in range(25, 30)])
        self.assertEqual(second.data["next"], None)
        self.assertNotIn("page=", second.data["previous"])
        self.assertIn("q=paging", second.data["previous"])

        past_the_end = self.client.get(URL, {"q": "paging", "page": 9})
        self.assertEqual(past_the_end.status_code, status.HTTP_200_OK)
        self.assertEqual(past_the_end.data["results"], [])

    def test_invalid_page(self):
        for page in ("0", "-1", "abc"):
            with self.subTest(page):
                response = self.client.get(URL, {"q": "club", "page": page})

                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_queries(self):
        for params in ({}, {"q": ""}, {"q": "sp"}, {"q": "x" * 101}):
            with self.subTest(params):
                response = self.client.get(URL, params)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("q", response.data)

    def test_project_wide_search_and_ordering_params_have_no_effect(self):
        response = self.client.get(URL, {"q": "tennis", "ordering": "-name", "search": "lisboa"})

        self.assertEqual(
            [r["name"] for r in response.data["results"]], ["Ibirapuera Tennis Center", "Lisboa Tennis Club"]
        )

    def test_requires_login(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.client.get(URL, {"q": "tennis"}).status_code, status.HTTP_403_FORBIDDEN)

    def test_read_only(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method):
                response = getattr(self.client, method)(URL, {"q": "x"}, format="json")

                self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
