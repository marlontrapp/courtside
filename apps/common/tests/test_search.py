"""Tests for the shared search building blocks (apps/common/search.py)."""

from django.test import TestCase

from apps.common.search import SearchQueryError, search_words


class SearchWordsTests(TestCase):
    def test_splits_on_anything_but_letters_and_digits(self):
        self.assertEqual(search_words("  Av. Paulista, 1000 - São_Paulo! "), ["Av", "Paulista", "1000", "São", "Paulo"])

    def test_drops_single_characters_and_duplicates(self):
        self.assertEqual(search_words("Rua 7 de Setembro rua"), ["Rua", "de", "Setembro"])

    def test_rejections(self):
        cases = {
            "": "Enter something to search for",
            "   ": "Enter something to search for",
            "sp": "at least one word with 3 or more",
            "a b c d": "at least one word with 3 or more",
            "%%% ___": "at least one word with 3 or more",
            "x" * 101: "too long",
            "one two three four five six seven eight nine": "at most 8 words",
        }
        for query, message in cases.items():
            with self.subTest(query), self.assertRaisesMessage(SearchQueryError, message):
                search_words(query)

    def test_subject_in_the_empty_query_message(self):
        with self.assertRaisesMessage(SearchQueryError, "Enter a player name or location to search for."):
            search_words(" ", subject="a player name or location")
