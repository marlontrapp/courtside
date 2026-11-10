"""Tests for the custom User manager/model and PlayerProfile."""

from django.db import IntegrityError
from django.test import TestCase

from apps.common.tests.factories import create_player_profile, create_user
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
        self.assertEqual(profile.membership_tier, PlayerProfile.MembershipTier.NONE)
        self.assertFalse(profile.is_member)

    def test_one_profile_per_user(self):
        profile = create_player_profile()

        with self.assertRaises(IntegrityError):
            PlayerProfile.objects.create(user=profile.user)

    def test_str_contains_email_and_skill_level(self):
        profile = create_player_profile(skill_level=PlayerProfile.SkillLevel.NTRP_4_5)

        self.assertEqual(str(profile), f"{profile.user.email} (4.5)")
