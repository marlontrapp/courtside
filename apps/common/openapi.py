"""
Shared pieces of the OpenAPI documentation (drf-spectacular): error bodies,
common responses and parameters. Each app's `openapi.py` documents its views;
`make openapi` writes the schema to docs/openapi.yaml.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse
from rest_framework import serializers

from .search import MAX_QUERY_LENGTH, MAX_WORDS, MIN_INDEXED_WORD_LENGTH


class DetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class NonFieldErrorsSerializer(serializers.Serializer):
    non_field_errors = serializers.ListField(child=serializers.CharField())


class RuleRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class UnlinkReportSerializer(serializers.Serializer):
    """Booking rules a forced deletion unlinked, and those deactivated rather than widened."""

    unlinked_booking_rules = RuleRefSerializer(many=True)
    deactivated_booking_rules = RuleRefSerializer(
        many=True, help_text="Left without any link of a kind they had: deactivated instead of covering more."
    )


VALIDATION_ERROR = OpenApiResponse(
    OpenApiTypes.OBJECT, description='Invalid input: `{"<field>": ["message", …]}` (or `non_field_errors`).'
)
NOT_LOGGED_IN = OpenApiResponse(DetailSerializer, description="Not logged in (or, for writes, no CSRF token).")
NOT_FOUND = OpenApiResponse(DetailSerializer, description="Not found.")
NOT_ADMIN = OpenApiResponse(
    DetailSerializer, description="Not found — or not a club you administer (club admins and site staff only)."
)
INVALID_PAGE = OpenApiResponse(DetailSerializer, description="`page` isn't a positive integer.")

FORCE_QUERY = OpenApiParameter(
    "force", OpenApiTypes.BOOL, OpenApiParameter.QUERY,
    description="Proceed anyway: cancel future bookings that no longer fit (reported in "
                "`cancelled_reservations`), or unlink booking rules (reported).",
)


def search_query(subject: str, examples: str) -> OpenApiParameter:
    return OpenApiParameter(
        "q", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True,
        description=(
            f"Words to find in the {subject}: every word must appear, whole or partial, ignoring case "
            f"and accents. Words are runs of letters/digits (punctuation separates them); 1-character "
            f"words are ignored; at least one word needs {MIN_INDEXED_WORD_LENGTH}+ characters (shorter "
            f"ones still filter); at most {MAX_WORDS} words and {MAX_QUERY_LENGTH} characters. Typos "
            f"aren't corrected. Examples: {examples}."
        ),
    )


SEARCH_RANKING = (
    "Best matches first: the name starts with the query, then the name contains it, then the name "
    "contains every word, then the rest; ties by name. Paginated **without a total count** (counting "
    "every match can cost as much as the search itself); a page past the end is empty."
)
SEARCH_BAD_QUERY = OpenApiResponse(
    OpenApiTypes.OBJECT,
    description='`q` missing/blank, too long, too many words, or no word of 3+ characters: `{"q": ["…"]}`.',
)
