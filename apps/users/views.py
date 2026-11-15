"""REST views for the logged-in player's profile."""

from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.pagination import NoCountPageNumberPagination
from apps.common.search import SearchQueryError

from . import openapi
from .search import search_players
from .serializers import PictureUploadSerializer, PlayerSearchResultSerializer
from .services import (
    PictureError,
    PictureTooLarge,
    picture_max_bytes,
    picture_too_large_message,
    picture_url,
    remove_player_picture,
    set_player_picture,
)

# Room for the multipart envelope (boundaries, part headers) around the file
# when checking Content-Length before reading the body.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


@openapi.my_profile_picture
class MyProfilePictureView(APIView):
    """
    PUT    /api/v1/me/profile/picture/ — set or replace the logged-in player's picture.
    DELETE /api/v1/me/profile/picture/ — remove it.

    PUT: multipart/form-data with one file field, `picture`: a still JPEG,
    PNG or WebP (detected from the content), at most
    settings.PLAYER_PICTURE_MAX_SIZE_KB. Stored re-encoded without metadata
    (EXIF/GPS, XMP, comments). The player profile is created if the user
    doesn't have one yet.
    200 {"picture_key", "picture_url"} | 400 missing file, not an image,
    unsupported format (GIF, ...), animated or corrupt |
    403 not logged in | 413 too large.
    Errors: {"picture": ["<message>"]}.

    DELETE: 204, also when there is no picture (safe to retry) | 403 not logged in.
    """

    parser_classes = [MultiPartParser]

    def put(self, request):
        # Refuse an oversized body before reading (and buffering) it.
        content_length = _content_length(request)
        if content_length is not None and content_length > picture_max_bytes() + _MULTIPART_OVERHEAD_BYTES:
            return _picture_error(picture_too_large_message(), status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        serializer = PictureUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            profile = set_player_picture(request.user, serializer.validated_data["picture"])
        except PictureTooLarge as exc:
            return _picture_error(str(exc), status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        except PictureError as exc:
            return _picture_error(str(exc), status.HTTP_400_BAD_REQUEST)

        return Response({
            "picture_key": profile.picture_key,
            "picture_url": picture_url(profile.picture_key, request),
        })

    def delete(self, request):
        remove_player_picture(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _content_length(request) -> int | None:
    try:
        return int(request.META.get("CONTENT_LENGTH") or "")
    except ValueError:
        return None


def _picture_error(message: str, status_code: int) -> Response:
    return Response({"picture": [message]}, status=status_code)


@openapi.player_search
class PlayerSearchView(generics.ListAPIView):
    """
    GET /api/v1/players/search/?q=<words> — find players by name and location.

    Every word must appear in the player's first/last name or location, whole
    or partial, ignoring case and accents; only active users with a player
    profile. Returns public details only (no email, phone, birth date,
    gender). Best matches first — see apps/common/search.py.

    Paginated without a total count: {"next", "previous", "results"}, ?page=N.

    200 results (possibly empty) | 400 missing/blank `q`, too long, too many
    words, or no word of 3+ letters/digits | 403 not logged in.
    """

    serializer_class = PlayerSearchResultSerializer
    pagination_class = NoCountPageNumberPagination
    filter_backends = []  # no ?search= / ?ordering= from the project defaults

    def get_queryset(self):
        try:
            return search_players(self.request.query_params.get("q", ""))
        except SearchQueryError as exc:
            raise ValidationError({"q": [str(exc)]}) from exc
