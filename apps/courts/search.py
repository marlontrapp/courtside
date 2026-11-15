"""
Club search by name and address — GET /api/v1/clubs/search/?q=...

Every word of the query must appear in the club's name or address (street,
number, neighbourhood, city, country, ... — all of it is one free-text
address), as a whole or partial word, ignoring case and accents:
"ibira sao paulo" finds "Ibirapuera Tennis Center — Av. Pedro Álvares
Cabral, São Paulo".

Club.search_document (name + address) and Club.search_name are stored
generated columns; the document has a partial GIN trigram index (active
clubs only). Matching and ranking: apps/common/search.py.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.common.search import filter_all_words, name_rank, search_words

from .models import Club

# Fields returned by the search; the rest of the row isn't read.
RESULT_FIELDS = ("id", "name", "slug", "address", "timezone", "access_type")


def search_clubs(query: str) -> QuerySet[Club]:
    """
    Live clubs — active and approved; open and members-only — matching every word of `query`,
    best matches first (see name_rank), then by name. Raises
    SearchQueryError for an unusable query.
    """
    words = search_words(query, subject="a club name or address")
    clubs = filter_all_words(Club.objects.active(), words, "search_document")  # live: active and approved
    return (
        clubs.only(*RESULT_FIELDS)
        .annotate(search_rank=name_rank(words, "search_name"))
        .order_by("search_rank", "name", "id")
    )
