"""
Player search by name and location — GET /api/v1/players/search/?q=...

Every word of the query must appear in the player's name (the user's first /
last name) or their profile's location, as a whole or partial word,
ignoring case and accents: "ana pinheiros" finds Ana Souza, "Pinheiros,
São Paulo". Email, phone and other private details are neither searched nor
returned.

PlayerProfile.search_document (name + location) and .search_name are kept
current by database triggers (users migration 0004) and the document has a
GIN trigram index. Matching and ranking: apps/common/search.py.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.common.search import filter_all_words, name_rank, search_words

from .models import PlayerProfile

# Columns read for the results (profile + its user, one query).
RESULT_FIELDS = ("location", "skill_level", "picture_key", "user__first_name", "user__last_name")


def search_players(query: str) -> QuerySet[PlayerProfile]:
    """
    Players (profiles of active users) matching every word of `query`, best
    matches first (see name_rank), then by name. Raises SearchQueryError for
    an unusable query.
    """
    words = search_words(query, subject="a player name or location")
    players = filter_all_words(PlayerProfile.objects.filter(user__is_active=True), words, "search_document")
    return (
        players.select_related("user")
        .only(*RESULT_FIELDS)
        .annotate(search_rank=name_rank(words, "search_name"))
        .order_by("search_rank", "search_name", "id")
    )
