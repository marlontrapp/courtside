"""Player profile services: profile picture upload and removal."""

from __future__ import annotations

import io
import logging
import uuid
import warnings
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage, storages
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import PlayerProfile, User

logger = logging.getLogger(__name__)

# Accepted picture formats: Pillow's format name -> file extension used in the
# storage key. Detected from the file's content, never from the client's
# filename or Content-Type. Still images only — animated pictures (animated
# PNG/WebP) are rejected; GIF isn't accepted.
PICTURE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
PICTURE_FORMAT_NAMES = "JPEG, PNG or WebP"

# Formats stored as another one. MPO is a JPEG with extra images appended
# (HDR gain map, depth map, ...), as many phones save photos: Pillow reports
# it as a multi-frame "MPO", but it's a still photo — only its main image is
# kept, stored as a plain JPEG.
_FORMAT_ALIASES = {"MPO": "JPEG"}

# Encoder options for the stored (re-encoded) picture, per format.
# Quality 90 is visually lossless for photos.
_SAVE_OPTIONS = {
    "JPEG": {"quality": 90},
    "PNG": {},
    "WEBP": {"quality": 90},
}

# Upper bound on width x height, checked from the header before decoding:
# a small, highly compressed file can still decode to a huge bitmap
# (a "decompression bomb"). 40 MP covers any camera-sized photo.
PICTURE_MAX_PIXELS = 40_000_000


class PictureError(Exception):
    """The uploaded file can't be used as a profile picture; str() is the message for the user."""


class PictureTooLarge(PictureError):
    pass


class InvalidPicture(PictureError):
    pass


def picture_storage() -> Storage:
    """Where profile pictures live (settings.STORAGES["player_pictures"])."""
    return storages["player_pictures"]


def picture_url(key: str, request=None) -> str | None:
    """Public URL of a stored picture (absolute when `request` is given); None without a picture."""
    if not key:
        return None
    url = picture_storage().url(key)
    return request.build_absolute_uri(url) if request is not None else url


def picture_max_bytes() -> int:
    return settings.PLAYER_PICTURE_MAX_SIZE_KB * 1024


def picture_too_large_message(size: int | None = None) -> str:
    limit = f"The maximum size is {settings.PLAYER_PICTURE_MAX_SIZE_KB} KB"
    if size is None:
        return f"The picture is too large. {limit}."
    return f"The picture is too large ({-(-size // 1024)} KB). {limit}."


@dataclass(frozen=True)
class PreparedPicture:
    content: bytes  # the re-encoded image, without metadata
    extension: str


def prepare_picture(file) -> PreparedPicture:
    """
    Check an uploaded file is a usable profile picture and return it
    re-encoded without metadata. Raises PictureTooLarge / InvalidPicture.

    The whole image is decoded, so truncated or corrupt files are rejected
    here rather than showing up broken in the apps. See `_strip_metadata`
    for what is kept.
    """
    if file.size > picture_max_bytes():
        raise PictureTooLarge(picture_too_large_message(file.size))

    file.seek(0)
    try:
        with warnings.catch_warnings():
            # Pillow only warns about very large images; treat it as an error.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(file) as image:
                image_format = _FORMAT_ALIASES.get(image.format, image.format)
                if image_format not in PICTURE_FORMATS:
                    raise InvalidPicture(
                        f"Unsupported image format ({image.format}). Use {PICTURE_FORMAT_NAMES}."
                    )
                if image.format not in _FORMAT_ALIASES and getattr(image, "is_animated", False):
                    raise InvalidPicture(
                        f"Animated pictures aren't supported. Use a still {PICTURE_FORMAT_NAMES} picture."
                    )
                width, height = image.size
                if width * height > PICTURE_MAX_PIXELS:
                    raise InvalidPicture(
                        f"The picture's dimensions are too large ({width}x{height} pixels). "
                        f"The maximum is {PICTURE_MAX_PIXELS // 1_000_000} megapixels."
                    )
                image.load()  # the first (for MPO: the main) image
                clean, save_options = _strip_metadata(image, image_format)
    except UnidentifiedImageError:
        raise InvalidPicture(f"The file isn't an image. Use {PICTURE_FORMAT_NAMES}.") from None
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise InvalidPicture("The picture's dimensions are too large.") from None
    except (OSError, SyntaxError, ValueError) as exc:  # Pillow's errors for corrupt/truncated data
        raise InvalidPicture("The image file is corrupt or incomplete.") from exc
    finally:
        file.seek(0)

    # Outside the try: an encoding failure is a bug on our side, not a bad upload.
    output = io.BytesIO()
    clean.save(output, format=image_format, **save_options)
    return PreparedPicture(content=output.getvalue(), extension=PICTURE_FORMATS[image_format])


def _strip_metadata(image: Image.Image, image_format: str) -> tuple[Image.Image, dict]:
    """
    A copy of the decoded image without metadata, and the options to save it
    with as `image_format`.

    Dropped: EXIF (camera, GPS location, date, ...), XMP, IPTC, comments and
    PNG text chunks — anything in `image.info`, which Pillow's encoders may
    otherwise copy into the output. The EXIF orientation is applied to the
    pixels first, so phone photos aren't shown sideways once it's gone.
    Kept: the ICC colour profile (without it, wide-gamut photos such as
    iPhone's Display P3 change colour) and transparency (PNG, WebP).
    """
    icc_profile = image.info.get("icc_profile")
    transparency = image.info.get("transparency")
    clean = ImageOps.exif_transpose(image)  # always a new image
    clean.info = {}
    options = dict(_SAVE_OPTIONS[image_format])
    if icc_profile:
        options["icc_profile"] = icc_profile
    # PNG palette / single-colour transparency; WebP transparency is an alpha channel (kept by the mode).
    if transparency is not None and image_format == "PNG":
        options["transparency"] = transparency
    return clean, options


def set_player_picture(user: User, file) -> PlayerProfile:
    """
    Validate `file`, strip its metadata, store it and make it the player's
    profile picture (creating the profile if the user has none yet).

    Every upload gets a new key, so CDN caches never serve a stale picture.
    The previous picture is deleted from storage once the change is
    committed; if saving fails, the new file is deleted instead.
    """
    picture = prepare_picture(file)
    storage = picture_storage()
    # Upload before locking the profile row: storage calls can be slow (S3).
    key = storage.save(
        f"players/{user.pk}/picture-{uuid.uuid4().hex}.{picture.extension}",
        ContentFile(picture.content),
    )
    try:
        with transaction.atomic():
            profile, _created = PlayerProfile.objects.get_or_create(user=user)
            # Serialize concurrent uploads by the same player, so each
            # replaced picture is deleted exactly once.
            profile = PlayerProfile.objects.select_for_update().get(pk=profile.pk)
            previous_key = profile.picture_key
            profile.picture_key = key
            profile.save(update_fields=["picture_key", "updated_at"])
    except BaseException:
        _delete_picture_quietly(key)
        raise
    if previous_key and previous_key != key:
        transaction.on_commit(lambda: _delete_picture_quietly(previous_key))
    return profile


def remove_player_picture(user: User) -> None:
    """
    Remove the player's profile picture, if any. Idempotent: no profile or no
    picture is not an error. The file is deleted from storage once the change
    is committed.
    """
    with transaction.atomic():
        profile = PlayerProfile.objects.select_for_update().filter(user=user).first()
        if profile is None or not profile.picture_key:
            return
        key = profile.picture_key
        profile.picture_key = ""
        profile.save(update_fields=["picture_key", "updated_at"])
    transaction.on_commit(lambda: _delete_picture_quietly(key))


def _delete_picture_quietly(key: str) -> None:
    """Best-effort delete: a leftover file only costs storage, so never fail the request."""
    try:
        picture_storage().delete(key)
    except Exception:
        logger.exception("Could not delete player picture %s from storage", key)
