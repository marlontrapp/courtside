"""Tests for Club, Court and ClubMembership models (schedules: test_schedule_*)."""

from datetime import timedelta

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.common.tests.factories import (
    create_club,
    create_club_membership,
    create_court,
    create_members_only_club,
    create_user,
)
from apps.courts.models import Club, ClubMembership, Court


class ClubTests(TestCase):
    def test_slug_is_auto_generated_from_name(self):
        club = create_club(name="Ibirapuera Tênis Clube")

        self.assertEqual(club.slug, "ibirapuera-tenis-clube")

    def test_explicit_slug_is_preserved(self):
        club = create_club(name="Ibirapuera Tênis Clube", slug="itc")

        self.assertEqual(club.slug, "itc")

    def test_slug_must_be_unique(self):
        create_club(name="Same Club")

        with self.assertRaises(IntegrityError):
            create_club(name="Same Club")

    def test_str_returns_name(self):
        club = create_club(name="Parque Clube")

        self.assertEqual(str(club), "Parque Clube")

    def test_clubs_are_open_by_default(self):
        club = create_club()

        self.assertEqual(club.access_type, Club.AccessType.OPEN)
        self.assertFalse(club.requires_membership)

    def test_members_only_club_requires_membership(self):
        self.assertTrue(create_members_only_club().requires_membership)

    def test_members_m2m_goes_through_membership(self):
        club = create_members_only_club()
        user = create_user()
        create_club_membership(user=user, club=club)

        self.assertQuerySetEqual(club.members.all(), [user])
        self.assertQuerySetEqual(user.clubs.all(), [club])


class CourtTests(TestCase):
    def test_court_name_unique_per_club(self):
        club = create_club()
        create_court(club=club, name="Court 1 - Guga")

        with self.assertRaises(IntegrityError):
            create_court(club=club, name="Court 1 - Guga")

    def test_same_court_name_allowed_across_clubs(self):
        create_court(club=create_club(), name="Center Court")
        other = create_court(club=create_club(), name="Center Court")

        self.assertEqual(other.name, "Center Court")

    def test_str_includes_club_and_court_name(self):
        court = create_court(club=create_club(name="Parque Clube"), name="Court 2")

        self.assertEqual(str(court), "Parque Clube · Court 2")


class ClubMembershipModelTests(TestCase):
    def test_defaults(self):
        membership = create_club_membership()

        self.assertEqual(membership.status, ClubMembership.Status.ACTIVE)
        self.assertEqual(membership.tier, ClubMembership.Tier.BASIC)
        self.assertIsNone(membership.expires_at)

    def test_one_membership_per_user_and_club(self):
        membership = create_club_membership()

        with self.assertRaises(IntegrityError):
            create_club_membership(user=membership.user, club=membership.club)

    def test_user_can_belong_to_many_clubs(self):
        user = create_user()
        clubs = [create_members_only_club() for _ in range(3)]
        for club in clubs:
            create_club_membership(user=user, club=club)

        self.assertEqual(user.clubs.count(), 3)

    def test_is_valid(self):
        now = timezone.now()
        cases = [
            (ClubMembership.Status.ACTIVE, None, True),
            (ClubMembership.Status.ACTIVE, now + timedelta(days=1), True),
            (ClubMembership.Status.ACTIVE, now - timedelta(seconds=1), False),
            (ClubMembership.Status.SUSPENDED, None, False),
            (ClubMembership.Status.CANCELLED, None, False),
        ]
        for status, expires_at, expected in cases:
            with self.subTest(status=status, expires_at=expires_at):
                membership = ClubMembership(status=status, expires_at=expires_at)
                self.assertIs(membership.is_valid(at=now), expected)

    def test_active_queryset_matches_is_valid(self):
        now = timezone.now()
        valid = create_club_membership()
        create_club_membership(expires_at=now - timedelta(days=1))
        create_club_membership(status=ClubMembership.Status.SUSPENDED)
        create_club_membership(status=ClubMembership.Status.CANCELLED)
        future = create_club_membership(expires_at=now + timedelta(days=30))

        self.assertCountEqual(ClubMembership.objects.active(), [valid, future])


class AccessibleToQuerySetTests(TestCase):
    """Club/Court `accessible_to(user)`: open clubs + clubs with active membership."""

    def setUp(self):
        self.user = create_user()
        self.open_club = create_club()
        self.member_club = create_members_only_club()
        self.other_private_club = create_members_only_club()
        create_club_membership(user=self.user, club=self.member_club)

        self.open_court = create_court(club=self.open_club)
        self.member_court = create_court(club=self.member_club)
        self.private_court = create_court(club=self.other_private_club)

    def test_courts_not_duplicated_with_multiple_courts_and_members(self):
        second_member_court = create_court(club=self.member_club)
        create_club_membership(user=create_user(), club=self.member_club)

        courts = list(Court.objects.accessible_to(self.user))

        self.assertEqual(len(courts), len(set(courts)))
        self.assertIn(second_member_court, courts)
