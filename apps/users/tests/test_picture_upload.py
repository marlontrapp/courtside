"""Tests for PUT/DELETE /api/v1/me/profile/picture/ and the picture services."""

import io
import re
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image, ImageCms, PngImagePlugin
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import create_player_profile, create_user
from apps.users.models import PlayerProfile
from apps.users.services import (
    InvalidPicture,
    PictureTooLarge,
    picture_storage,
    prepare_picture,
    remove_player_picture,
    set_player_picture,
)

URL = reverse("users:my-profile-picture")


def image_bytes(fmt: str = "PNG", size=(64, 64), **save_kwargs) -> bytes:
    buffer = io.BytesIO()
    mode = "P" if fmt == "GIF" else "RGB"
    Image.new(mode, size, color=1 if mode == "P" else (200, 80, 20)).save(buffer, format=fmt, **save_kwargs)
    return buffer.getvalue()


def _encode(image: Image.Image, fmt: str, **save_kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **save_kwargs)
    return buffer.getvalue()


def animated_bytes(fmt: str) -> bytes:
    """A 3-frame animation (red, green, blue)."""
    frames = [Image.new("RGB", (16, 16), colour) for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    buffer = io.BytesIO()
    frames[0].save(buffer, fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
    assert Image.open(io.BytesIO(buffer.getvalue())).n_frames == 3
    return buffer.getvalue()


def mpo_bytes(**save_kwargs) -> bytes:
    """A phone-style multi-picture JPEG: a 60x40 red photo + a 30x20 black auxiliary image."""
    buffer = io.BytesIO()
    Image.new("RGB", (60, 40), (200, 0, 0)).save(
        buffer, "MPO", save_all=True, append_images=[Image.new("RGB", (30, 20))], **save_kwargs
    )
    return buffer.getvalue()


def upload(content: bytes, name: str = "me.png", content_type: str = "image/png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type=content_type)


def noise_png(size=(200, 200)) -> bytes:
    """An incompressible PNG, for tests that need a file above a size."""
    import os

    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buffer, format="PNG")
    return buffer.getvalue()


def stored(key: str) -> bool:
    return picture_storage().exists(key)


class PictureUploadAPITests(APITestCase):
    def setUp(self):
        self.user = create_user()
        self.client.force_authenticate(self.user)

    def put(self, file, **extra):
        return self.client.put(URL, {"picture": file}, format="multipart", **extra)

    def test_uploads_picture_and_returns_key_and_url(self):
        content = image_bytes("PNG")

        response = self.put(upload(content))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        key = response.data["picture_key"]
        self.assertRegex(key, rf"^players/{self.user.pk}/picture-[0-9a-f]{{32}}\.png$")
        self.assertEqual(response.data["picture_url"], f"http://testserver/media/{key}")  # MEDIA_URL
        self.assertEqual(PlayerProfile.objects.get(user=self.user).picture_key, key)
        with picture_storage().open(key) as f, Image.open(f) as saved:  # re-encoded, same picture
            self.assertEqual((saved.format, saved.size), ("PNG", (64, 64)))
            self.assertEqual(saved.getpixel((0, 0)), (200, 80, 20))

    def test_accepts_common_formats(self):
        for fmt, ext in (("JPEG", "jpg"), ("PNG", "png"), ("WEBP", "webp")):
            with self.subTest(fmt):
                response = self.put(upload(image_bytes(fmt), name=f"me.{ext}"))

                self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
                self.assertTrue(response.data["picture_key"].endswith(f".{ext}"))

    def test_format_comes_from_content_not_from_name_or_content_type(self):
        response = self.put(upload(image_bytes("PNG"), name="me.jpg", content_type="image/jpeg"))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["picture_key"].endswith(".png"))

    def test_rejects_other_image_formats(self):
        for fmt in ("GIF", "BMP", "TIFF"):  # GIF also when it's a still image
            with self.subTest(fmt):
                response = self.put(upload(image_bytes(fmt), name="me.png"))

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.data, {"picture": [f"Unsupported image format ({fmt}). Use JPEG, PNG or WebP."]}
                )

    def test_rejects_animated_pictures(self):
        for fmt in ("PNG", "WEBP", "GIF"):
            with self.subTest(fmt):
                response = self.put(upload(animated_bytes(fmt), name="me.png"))

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                expected = (
                    f"Unsupported image format ({fmt}). Use JPEG, PNG or WebP."
                    if fmt == "GIF"
                    else "Animated pictures aren't supported. Use a still JPEG, PNG or WebP picture."
                )
                self.assertEqual(response.data, {"picture": [expected]})
        self.assertFalse(PlayerProfile.objects.filter(user=self.user).exists())

    def test_accepts_a_still_webp(self):
        for label, content in (
            ("lossy", image_bytes("WEBP")),
            ("lossless with alpha", _encode(Image.new("RGBA", (16, 16), (0, 0, 255, 128)), "WEBP", lossless=True)),
        ):
            with self.subTest(label):
                self.assertFalse(Image.open(io.BytesIO(content)).is_animated)

                response = self.put(upload(content, name="me.webp", content_type="image/webp"))

                self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
                self.assertTrue(response.data["picture_key"].endswith(".webp"))

    def test_accepts_a_phone_jpeg_with_extra_images_and_keeps_only_the_photo(self):
        # MPO: a JPEG + appended images (HDR gain map, depth map), as many phones
        # save photos. Pillow sees several frames, but it's a still photo.
        response = self.put(upload(mpo_bytes(), name="IMG_0001.jpg", content_type="image/jpeg"))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        key = response.data["picture_key"]
        self.assertTrue(key.endswith(".jpg"))
        with picture_storage().open(key) as f, Image.open(f) as saved:
            self.assertEqual((saved.format, saved.size), ("JPEG", (60, 40)))
            self.assertEqual(getattr(saved, "n_frames", 1), 1)
            self.assertNotIn("mp", saved.info)

    def test_rejects_a_file_that_is_not_an_image(self):
        for content in (b"<html><script>alert(1)</script></html>", b"%PDF-1.7 not a picture"):
            with self.subTest(content[:10]):
                response = self.put(upload(content, name="me.jpg", content_type="image/jpeg"))

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.data, {"picture": ["The file isn't an image. Use JPEG, PNG or WebP."]}
                )

    def test_rejects_a_truncated_image(self):
        for fmt in ("JPEG", "PNG"):
            with self.subTest(fmt):
                content = image_bytes(fmt, size=(300, 300))

                response = self.put(upload(content[: len(content) // 2]))

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data, {"picture": ["The image file is corrupt or incomplete."]})

    def test_nothing_is_stored_when_rejected(self):
        with mock.patch.object(type(picture_storage()), "save") as save:
            self.put(upload(b"not an image"))

        save.assert_not_called()
        self.assertFalse(PlayerProfile.objects.filter(user=self.user).exists())

    def test_missing_or_empty_file(self):
        for data in ({}, {"picture": "not-a-file"}, {"picture": upload(b"")}):
            with self.subTest(data):
                response = self.client.put(URL, data, format="multipart")

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("picture", response.data)

    @override_settings(PLAYER_PICTURE_MAX_SIZE_KB=10)
    def test_rejects_a_picture_over_the_size_limit(self):
        content = noise_png((70, 70))  # ~14.7 KB
        self.assertGreater(len(content), 10 * 1024)

        response = self.put(upload(content))

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        kb = -(-len(content) // 1024)
        self.assertEqual(
            response.data, {"picture": [f"The picture is too large ({kb} KB). The maximum size is 10 KB."]}
        )
        self.assertFalse(PlayerProfile.objects.filter(user=self.user).exists())

    def test_accepts_a_picture_exactly_at_the_size_limit(self):
        content = noise_png((40, 40))
        limit_kb = len(content) / 1024

        with override_settings(PLAYER_PICTURE_MAX_SIZE_KB=int(limit_kb) + 1):
            padded = content + b"\0" * ((int(limit_kb) + 1) * 1024 - len(content))  # trailing bytes after IEND
            response = self.put(upload(padded))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @override_settings(PLAYER_PICTURE_MAX_SIZE_KB=10)
    def test_rejects_an_oversized_body_before_reading_it(self):
        with mock.patch("apps.users.views.set_player_picture") as service:
            response = self.put(upload(image_bytes()), CONTENT_LENGTH=str(10 * 1024 + 64 * 1024 + 1))

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(response.data, {"picture": ["The picture is too large. The maximum size is 10 KB."]})
        service.assert_not_called()

    def test_rejects_huge_dimensions(self):
        with mock.patch("apps.users.services.PICTURE_MAX_PIXELS", 64 * 64 - 1):
            response = self.put(upload(image_bytes(size=(64, 64))))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("dimensions are too large (64x64 pixels)", response.data["picture"][0])

    def test_rejects_decompression_bombs_flagged_by_pillow(self):
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 100):
            response = self.put(upload(image_bytes(size=(20, 20))))  # 400 px > MAX_IMAGE_PIXELS

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"picture": ["The picture's dimensions are too large."]})

    def test_replacing_deletes_the_previous_picture_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = self.put(upload(image_bytes())).data["picture_key"]
        self.assertTrue(stored(first))

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            second = self.put(upload(image_bytes("JPEG"))).data["picture_key"]
        self.assertNotEqual(first, second)
        self.assertTrue(stored(first), "deleted before commit")

        for callback in callbacks:
            callback()
        self.assertFalse(stored(first))
        self.assertTrue(stored(second))

    def test_creates_the_profile_or_keeps_the_existing_data(self):
        profile = create_player_profile(user=self.user, racket="Head Prestige", skill_level="4.5")

        self.put(upload(image_bytes()))

        profile.refresh_from_db()
        self.assertNotEqual(profile.picture_key, "")
        self.assertEqual(profile.racket, "Head Prestige")
        self.assertEqual(profile.skill_level, "4.5")

    def test_requires_login(self):
        self.client.force_authenticate(None)

        response = self.put(upload(image_bytes()))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_only_put_with_multipart(self):
        self.assertEqual(self.client.post(URL, {}).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        response = self.client.put(URL, {"picture": "x"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)


class SetPlayerPictureServiceTests(TestCase):
    def test_new_file_is_deleted_when_saving_the_profile_fails(self):
        user = create_user()
        storage = picture_storage()
        saved_keys = []
        real_save = storage.save

        def tracking_save(name, content, **kwargs):
            saved_keys.append(real_save(name, content, **kwargs))
            return saved_keys[-1]

        with (
            mock.patch.object(storage, "save", side_effect=tracking_save),
            mock.patch.object(PlayerProfile, "save", side_effect=RuntimeError("db down")),
            self.assertRaises(RuntimeError),
        ):
            set_player_picture(user, upload(image_bytes()))

        self.assertEqual(len(saved_keys), 1)
        self.assertFalse(stored(saved_keys[0]))

    def test_failing_to_delete_the_old_picture_is_logged_not_raised(self):
        user = create_user()
        with self.captureOnCommitCallbacks(execute=True):
            old = set_player_picture(user, upload(image_bytes())).picture_key

        with (
            mock.patch.object(picture_storage(), "delete", side_effect=OSError("S3 unavailable")),
            self.assertLogs("apps.users.services", level="ERROR") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            new = set_player_picture(user, upload(image_bytes())).picture_key

        self.assertIn(old, logs.output[0])
        self.assertEqual(PlayerProfile.objects.get(user=user).picture_key, new)

    def test_prepare_picture_rewinds_the_file(self):
        file = upload(image_bytes("JPEG"))

        self.assertEqual(prepare_picture(file).extension, "jpg")
        self.assertEqual(file.tell(), 0)

    @override_settings(PLAYER_PICTURE_MAX_SIZE_KB=1)
    def test_prepare_picture_checks_size_before_decoding(self):
        with mock.patch("apps.users.services.Image.open") as image_open, self.assertRaises(PictureTooLarge):
            prepare_picture(upload(b"x" * 1025))

        image_open.assert_not_called()

    def test_prepare_picture_error_types(self):
        with self.assertRaises(InvalidPicture):
            prepare_picture(upload(b"plain text"))

    def test_keys_are_unique_per_upload(self):
        user = create_user()
        keys = {set_player_picture(user, upload(image_bytes())).picture_key for _ in range(3)}

        self.assertEqual(len(keys), 3)
        for key in keys:
            self.assertTrue(re.fullmatch(rf"players/{user.pk}/picture-[0-9a-f]{{32}}\.png", key))


# --- metadata stripping -------------------------------------------------------------

# Markers planted in the test files' metadata; none may reach the stored file.
SECRETS = (b"SECRET", b"iPhone", b"Apple")
XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF>'
    b'<rdf:Description GPSLatitude="23,33S" note="SECRET-XMP"/></rdf:RDF></x:xmpmeta>'
)
ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def phone_exif(orientation: int = 1) -> bytes:
    """EXIF like a phone photo's: camera, GPS position (São Paulo), orientation."""
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15"
    exif[0x8825] = {1: "S", 2: (23.0, 33.0, 1.0), 3: "W", 4: (46.0, 38.0, 1.0)}  # GPS IFD
    return exif.tobytes()


def stripped(content: bytes, name: str = "me.jpg") -> tuple[bytes, Image.Image]:
    """(stored bytes, opened stored image) for an upload."""
    picture = prepare_picture(upload(content, name=name))
    return picture.content, Image.open(io.BytesIO(picture.content))


class PictureMetadataTests(TestCase):
    def assertNoMetadata(self, content: bytes, image: Image.Image, keep=()):
        for secret in SECRETS:
            self.assertNotIn(secret, content)
        self.assertEqual(len(image.getexif()), 0, "EXIF left")
        self.assertNotIn("xmp", image.info)
        self.assertNotIn("comment", image.info)
        self.assertEqual([k for k in image.info if k in ("exif", "xmp", "comment", "Author")], [])

    def test_jpeg_loses_exif_gps_xmp_and_comment(self):
        source = image_bytes("JPEG", exif=phone_exif(), xmp=XMP, comment=b"SECRET-COMMENT")
        self.assertIn(b"iPhone", source)
        self.assertIn(b"SECRET", source)

        content, image = stripped(source)

        self.assertNoMetadata(content, image)
        self.assertEqual(image.format, "JPEG")

    def test_png_loses_exif_and_text_chunks(self):
        text = PngImagePlugin.PngInfo()
        text.add_text("Author", "SECRET-AUTHOR")
        text.add_itxt("XML:com.adobe.xmp", XMP.decode())

        content, image = stripped(image_bytes("PNG", exif=phone_exif(), pnginfo=text), "me.png")

        self.assertNoMetadata(content, image)

    def test_webp_loses_exif_and_xmp(self):
        content, image = stripped(image_bytes("WEBP", exif=phone_exif(), xmp=XMP), "me.webp")

        self.assertNoMetadata(content, image)
        self.assertEqual(image.format, "WEBP")

    def test_phone_jpeg_with_extra_images_loses_exif_gps(self):
        source = mpo_bytes(exif=phone_exif())
        self.assertIn(b"iPhone", source)

        content, image = stripped(source)

        self.assertNoMetadata(content, image)
        self.assertEqual(image.format, "JPEG")

    def test_colour_profile_is_kept(self):
        for fmt in ("JPEG", "PNG", "WEBP"):
            with self.subTest(fmt):
                _content, image = stripped(image_bytes(fmt, icc_profile=ICC, exif=phone_exif()))

                self.assertEqual(image.info.get("icc_profile"), ICC)

    def test_orientation_is_applied_to_the_pixels(self):
        # Stored landscape: left half red, right half blue. Orientation 6 = display
        # rotated 90° clockwise, so the player sees a portrait: red on top.
        stored = Image.new("RGB", (40, 20), (0, 0, 255))
        stored.paste((255, 0, 0), (0, 0, 20, 20))
        buffer = io.BytesIO()
        stored.save(buffer, "JPEG", quality=95, exif=phone_exif(orientation=6))

        content, image = stripped(buffer.getvalue())

        self.assertEqual(image.size, (20, 40))
        self.assertEqual(len(image.getexif()), 0)
        red, _, blue = image.getpixel((10, 5))
        self.assertGreater(red, 200)
        self.assertLess(blue, 60)
        red, _, blue = image.getpixel((10, 35))
        self.assertLess(red, 60)
        self.assertGreater(blue, 200)

    def test_transparency_is_kept(self):
        rgba = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        rgba.paste((255, 0, 0, 255), (0, 0, 5, 10))
        for label, fmt, source, save in (
            ("PNG alpha channel", "PNG", rgba, {}),
            ("PNG paletted", "PNG", rgba.convert("P"), {"transparency": 0}),  # a palette index
            ("WebP alpha channel", "WEBP", rgba, {"lossless": True}),
        ):
            with self.subTest(label):
                buffer = io.BytesIO()
                source.save(buffer, fmt, **save)

                _content, image = stripped(buffer.getvalue(), "me.png")

                self.assertEqual(image.convert("RGBA").getpixel((8, 5))[3], 0, "transparent area")
                self.assertEqual(image.convert("RGBA").getpixel((2, 5))[3], 255, "opaque area")

    def test_corrupt_exif_doesnt_reject_a_good_picture(self):
        jpeg = image_bytes("JPEG")
        garbage_app1 = b"\xff\xe1" + (2 + 6 + 10).to_bytes(2, "big") + b"Exif\0\0" + b"\x01\x02garbage!"
        content, image = stripped(jpeg[:2] + garbage_app1 + jpeg[2:])

        self.assertEqual(image.size, (64, 64))
        self.assertNotIn(b"garbage", content)

    def test_upload_stores_the_stripped_file(self):
        user = create_user()
        source = image_bytes("JPEG", exif=phone_exif(), xmp=XMP, comment=b"SECRET")

        key = set_player_picture(user, upload(source)).picture_key

        with picture_storage().open(key) as f:
            stored_content = f.read()
        self.assertNoMetadata(stored_content, Image.open(io.BytesIO(stored_content)))


# --- DELETE ---------------------------------------------------------------------------


class PictureDeleteAPITests(APITestCase):
    def setUp(self):
        self.user = create_user()
        self.client.force_authenticate(self.user)

    def upload_picture(self, user=None) -> str:
        with self.captureOnCommitCallbacks(execute=True):
            return set_player_picture(user or self.user, upload(image_bytes())).picture_key

    def test_removes_the_picture_and_deletes_the_file_after_commit(self):
        key = self.upload_picture()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.delete(URL)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response.content, b"")
        self.assertEqual(PlayerProfile.objects.get(user=self.user).picture_key, "")
        self.assertTrue(stored(key), "deleted before commit")
        for callback in callbacks:
            callback()
        self.assertFalse(stored(key))

    def test_keeps_the_rest_of_the_profile(self):
        self.upload_picture()
        PlayerProfile.objects.filter(user=self.user).update(racket="Wilson Blade 98", skill_level="4.0")

        self.client.delete(URL)

        profile = PlayerProfile.objects.get(user=self.user)
        self.assertEqual((profile.picture_key, profile.racket, profile.skill_level), ("", "Wilson Blade 98", "4.0"))

    def test_is_idempotent(self):
        self.upload_picture()
        self.assertEqual(self.client.delete(URL).status_code, status.HTTP_204_NO_CONTENT)

        with self.captureOnCommitCallbacks() as callbacks:
            response = self.client.delete(URL)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(callbacks, [], "nothing to delete the second time")

    def test_without_profile_is_a_no_op(self):
        response = self.client.delete(URL)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PlayerProfile.objects.filter(user=self.user).exists(), "no profile created")

    def test_leaves_other_players_pictures_alone(self):
        other = create_user()
        other_key = self.upload_picture(user=other)
        self.upload_picture()

        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(URL)

        self.assertEqual(PlayerProfile.objects.get(user=other).picture_key, other_key)
        self.assertTrue(stored(other_key))

    def test_upload_after_delete(self):
        self.upload_picture()
        self.client.delete(URL)

        response = self.client.put(URL, {"picture": upload(image_bytes())}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(PlayerProfile.objects.get(user=self.user).picture_key, response.data["picture_key"])

    def test_requires_login(self):
        key = self.upload_picture()
        self.client.force_authenticate(None)

        self.assertEqual(self.client.delete(URL).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(PlayerProfile.objects.get(user=self.user).picture_key, key)

    def test_failing_to_delete_the_file_is_logged_not_raised(self):
        key = self.upload_picture()

        with (
            mock.patch.object(picture_storage(), "delete", side_effect=OSError("S3 unavailable")),
            self.assertLogs("apps.users.services", level="ERROR") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.delete(URL)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIn(key, logs.output[0])
        self.assertEqual(PlayerProfile.objects.get(user=self.user).picture_key, "")


class RemovePlayerPictureServiceTests(TestCase):
    def test_locks_the_profile_row(self):
        user = create_user()
        set_player_picture(user, upload(image_bytes()))

        with mock.patch.object(PlayerProfile.objects, "select_for_update", wraps=PlayerProfile.objects.select_for_update) as lock:
            remove_player_picture(user)

        lock.assert_called_once()
