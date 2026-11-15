"""
Building blocks shared by the text searches (clubs, players).

A searchable model keeps, maintained by PostgreSQL on every write:
- a normalized "document" column (accents removed, lower case) with a GIN
  trigram index (pg_trgm, gin_trgm_ops), and
- a normalized name column, used for ranking.

A query is split into words; every word must appear (whole or partial) in
the document — `document LIKE '%word%'`, served by the trigram index. Search
words are normalized by the same SQL function as the stored text; it's
IMMUTABLE, so the planner folds them into constant patterns and can use the
index. Words shorter than 3 characters have no trigram to look up, so a
query needs at least one word of 3+ characters; shorter words still filter.
"""

from __future__ import annotations

import re

from django.db import models
from django.db.models import Case, Func, IntegerField, Q, QuerySet, Value, When

MAX_QUERY_LENGTH = 100
MAX_WORDS = 8
# Shorter words (initials, "e", "a", ...) are ignored.
MIN_WORD_LENGTH = 2
# The trigram index needs at least one word this long to narrow the search.
MIN_INDEXED_WORD_LENGTH = 3

# Letters and digits of any script; punctuation and "_" separate words, so
# LIKE wildcards (% and _) can never reach the pattern.
_WORD_RE = re.compile(r"[^\W_]+")


class SearchNormalize(Func):
    """
    Text as stored for search and compared with search terms: accents
    removed, lower case ("São Paulo" -> "sao paulo"). Calls the IMMUTABLE SQL
    function created in courts migration 0005 (unaccent() itself isn't
    immutable, so it can't be used in generated columns or indexes).
    """

    function = "courtcontrol_search_normalize"
    output_field = models.TextField()


class SearchQueryError(ValueError):
    """The query can't be searched; str() is the message for the user."""


def search_words(query: str, *, subject: str = "something") -> list[str]:
    """
    The words of `query` to search for, in order, without duplicates.
    `subject` completes the "Enter ... to search for." message.
    """
    query = query.strip()
    if not query:
        raise SearchQueryError(f"Enter {subject} to search for.")
    if len(query) > MAX_QUERY_LENGTH:
        raise SearchQueryError(f"The search is too long (max {MAX_QUERY_LENGTH} characters).")

    words: list[str] = []
    for word in _WORD_RE.findall(query):
        if len(word) >= MIN_WORD_LENGTH and word.casefold() not in (w.casefold() for w in words):
            words.append(word)
    if len(words) > MAX_WORDS:
        raise SearchQueryError(f"Use at most {MAX_WORDS} words.")
    if not any(len(word) >= MIN_INDEXED_WORD_LENGTH for word in words):
        raise SearchQueryError(
            f"Include at least one word with {MIN_INDEXED_WORD_LENGTH} or more letters or digits."
        )
    return words


def filter_all_words(queryset: QuerySet, words: list[str], document_field: str) -> QuerySet:
    """Rows whose `document_field` contains every word (normalized)."""
    for word in words:
        queryset = queryset.filter(**{f"{document_field}__contains": SearchNormalize(Value(word))})
    return queryset


def name_rank(words: list[str], name_field: str) -> Case:
    """
    Cheap relevance tier from the normalized name (lower is better):

      0. the name starts with the query   ("ibirapuera ten")
      1. the name contains the query      ("tennis center")
      2. the name contains every word     ("center ibirapuera")
      3. the rest (words matched elsewhere in the document)

    A CASE on stored text: per-row similarity scoring costs ~10x more.
    """
    phrase = SearchNormalize(Value(" ".join(words)))
    normalized = [SearchNormalize(Value(word)) for word in words]
    return Case(
        When(**{f"{name_field}__startswith": phrase}, then=Value(0)),
        When(**{f"{name_field}__contains": phrase}, then=Value(1)),
        When(Q(*[Q(**{f"{name_field}__contains": word}) for word in normalized]), then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
