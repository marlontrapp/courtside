"""Tests for the custom User manager/model and PlayerProfile."""

from datetime import date
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from apps.common.tests.factories import create_player_profile, create_superuser, create_user
from apps.users.models import PlayerProfile, User


class UserManagerTests(TestCase):
    def test_create_user_with_email(self):
        user = User.objects.create_user(email="ana@example.com", password="s3cret-pass!")

        self.assertEqual(user.email, "ana@example.com")
        self.assertTrue(user.check_password("s3cret-pass!"))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_create_user_normalizes_email_domain(self):
        user = User.objects.create_user(email="ana@EXAMPLE.COM", password="s3cret-pass!")

        self.assertEqual(user.email, "ana@example.com")

    def test_create_user_without_email_raises(self):
        with self.assertRaisesMessage(ValueError, "email address must be set"):
            User.objects.create_user(email="", password="s3cret-pass!")

    def test_create_superuser_sets_flags(self):
        admin = User.objects.create_superuser(email="root@example.com", password="s3cret-pass!")

        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_active)

    def test_create_superuser_rejects_is_staff_false(self):
        with self.assertRaisesMessage(ValueError, "is_staff=True"):
            User.objects.create_superuser(
                email="root@example.com", password="s3cret-pass!", is_staff=False
            )

    def test_create_superuser_rejects_is_superuser_false(self):
        with self.assertRaisesMessage(ValueError, "is_superuser=True"):
            User.objects.create_superuser(
                email="root@example.com", password="s3cret-pass!", is_superuser=False
            )


class UserModelTests(TestCase):
    def test_email_is_username_field(self):
        self.assertEqual(User.USERNAME_FIELD, "email")
        self.assertEqual(User.REQUIRED_FIELDS, [])

    def test_email_must_be_unique(self):
        create_user(email="dup@example.com")

        with self.assertRaises(IntegrityError):
            create_user(email="dup@example.com")

    def test_str_returns_email(self):
        user = create_user(email="guga@example.com")

        self.assertEqual(str(user), "guga@example.com")


class PlayerProfileTests(TestCase):
    def test_defaults(self):
        profile = create_player_profile()

        self.assertEqual(profile.skill_level, PlayerProfile.SkillLevel.NTRP_3_0)

    def test_one_profile_per_user(self):
        profile = create_player_profile()

        with self.assertRaises(IntegrityError):
            PlayerProfile.objects.create(user=profile.user)

    def test_str_contains_email_and_skill_level(self):
        profile = create_player_profile(skill_level=PlayerProfile.SkillLevel.NTRP_4_5)

        self.assertEqual(str(profile), f"{profile.user.email} (4.5)")


class PlayerProfileDetailsTests(TestCase):
    """Birth date, gender, dominant hand, backhand, racket, preferred surface, picture key."""

    def test_details_are_optional(self):
        profile = create_player_profile()

        self.assertIsNone(profile.birth_date)
        self.assertEqual(profile.gender, "")
        self.assertEqual(profile.dominant_hand, "")
        self.assertEqual(profile.backhand_style, "")
        self.assertEqual(profile.racket, "")
        self.assertEqual(profile.preferred_surface, "")
        self.assertEqual(profile.picture_key, "")
        profile.full_clean()  # does not raise

    def test_stores_full_details(self):
        profile = create_player_profile(
            birth_date=date(1976, 9, 10),
            gender=PlayerProfile.Gender.MALE,
            dominant_hand=PlayerProfile.DominantHand.RIGHT,
            backhand_style=PlayerProfile.BackhandStyle.ONE_HANDED,
            racket="Head Prestige",
            preferred_surface="CLAY",
            picture_key="players/guga/avatar.jpg",
        )
        profile.full_clean()  # does not raise

        profile.refresh_from_db()
        self.assertEqual(profile.birth_date, date(1976, 9, 10))
        self.assertEqual(profile.gender, "MALE")
        self.assertEqual(profile.dominant_hand, "RIGHT")
        self.assertEqual(profile.backhand_style, "ONE_HANDED")
        self.assertEqual(profile.racket, "Head Prestige")
        self.assertEqual(profile.preferred_surface, "CLAY")
        self.assertEqual(profile.picture_key, "players/guga/avatar.jpg")

    def test_database_rejects_unknown_choices(self):
        profile = create_player_profile()
        for field in ("gender", "dominant_hand", "backhand_style", "preferred_surface"):
            with self.subTest(field), self.assertRaises(IntegrityError), transaction.atomic():
                PlayerProfile.objects.filter(pk=profile.pk).update(**{field: "BOGUS"})

    def test_every_gender_choice_is_accepted(self):
        for gender in PlayerProfile.Gender.values:
            with self.subTest(gender):
                create_player_profile(gender=gender).full_clean()  # does not raise

    def test_database_rejects_birth_date_before_1900(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_player_profile(birth_date=date(1899, 12, 31))

        create_player_profile(birth_date=date(1900, 1, 1))  # the floor itself is allowed

    def test_birth_date_in_the_future_is_invalid(self):
        with mock.patch("django.utils.timezone.localdate", return_value=date(2030, 1, 8)):
            create_player_profile(birth_date=date(2030, 1, 8)).full_clean()  # today is fine

            with self.assertRaises(ValidationError) as ctx:
                create_player_profile(birth_date=date(2030, 1, 9)).full_clean()

        self.assertIn("birth_date", ctx.exception.message_dict)

    def test_picture_key_must_not_be_a_url_or_absolute_path(self):
        for key in ("https://cdn.example.com/players/a.jpg", "s3://bucket/players/a.jpg", "/players/a.jpg"):
            with self.subTest(key), self.assertRaises(ValidationError) as ctx:
                create_player_profile(picture_key=key).full_clean()
            self.assertIn("picture_key", ctx.exception.message_dict)

    def test_picture_key_accepts_long_storage_keys(self):
        key = "players/" + "a" * 1000 + ".jpg"  # S3 keys go up to 1024 bytes

        create_player_profile(picture_key=key).full_clean()  # does not raise

    def test_full_name_comes_from_the_user(self):
        user = create_user(first_name="Gustavo", last_name="Kuerten")

        self.assertEqual(create_player_profile(user=user).full_name, "Gustavo Kuerten")
        self.assertEqual(create_player_profile().full_name, "")

    def test_age(self):
        profile = create_player_profile(birth_date=date(2000, 6, 15))

        self.assertEqual(profile.age(on=date(2030, 6, 14)), 29)
        self.assertEqual(profile.age(on=date(2030, 6, 15)), 30)
        self.assertEqual(profile.age(on=date(2000, 6, 15)), 0)

    def test_age_of_a_leap_day_birth_date(self):
        profile = create_player_profile(birth_date=date(2008, 2, 29))

        self.assertEqual(profile.age(on=date(2030, 2, 28)), 21)
        self.assertEqual(profile.age(on=date(2030, 3, 1)), 22)
        self.assertEqual(profile.age(on=date(2032, 2, 29)), 24)

    def test_age_defaults_to_today(self):
        profile = create_player_profile(birth_date=date(2000, 6, 15))

        with mock.patch("django.utils.timezone.localdate", return_value=date(2030, 6, 15)):
            self.assertEqual(profile.age(), 30)

    def test_age_without_birth_date(self):
        self.assertIsNone(create_player_profile().age())


class PlayerProfileAdminTests(TestCase):
    def setUp(self):
        self.client.force_login(create_superuser())

    def test_changelist_and_filters_render(self):
        create_player_profile(
            user=create_user(first_name="Ana", last_name="Silva"),
            gender="FEMALE",
            dominant_hand="LEFT",
            backhand_style="TWO_HANDED",
        )
        url = reverse("admin:users_playerprofile_changelist")

        response = self.client.get(
            url, {"dominant_hand__exact": "LEFT", "gender__exact": "FEMALE", "q": "Silva"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ana Silva")

    def test_edit_details(self):
        profile = create_player_profile()
        url = reverse("admin:users_playerprofile_change", args=[profile.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

        response = self.client.post(url, {
            "user": profile.user.pk,
            "phone_number": "",
            "birth_date": "1990-05-20",
            "gender": "FEMALE",
            "picture_key": "players/x/avatar.png",
            "skill_level": "4.0",
            "dominant_hand": "RIGHT",
            "backhand_style": "TWO_HANDED",
            "racket": "Wilson Blade 98",
            "preferred_surface": "HARD",
        })

        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.birth_date, date(1990, 5, 20))
        self.assertEqual(profile.gender, "FEMALE")
        self.assertEqual(profile.backhand_style, "TWO_HANDED")
        self.assertEqual(profile.racket, "Wilson Blade 98")

    def test_admin_rejects_a_picture_url(self):
        profile = create_player_profile()
        url = reverse("admin:users_playerprofile_change", args=[profile.pk])

        response = self.client.post(url, {
            "user": profile.user.pk,
            "skill_level": "3.0",
            "picture_key": "https://cdn.example.com/a.jpg",
        })

        self.assertEqual(response.status_code, 200)  # form re-rendered with the error
        self.assertContains(response, "not a URL")

    def test_user_page_with_profile_inline_renders(self):
        profile = create_player_profile(birth_date=date(1990, 5, 20))

        response = self.client.get(reverse("admin:users_user_change", args=[profile.user.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "backhand_style")
