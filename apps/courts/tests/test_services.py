"""Tests for club access/visibility and membership management services."""

from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.utils import timezone

from apps.common.tests.factories import (
    create_club,
    create_club_membership,
    create_court,
    create_members_only_club,
    create_user,
)
from apps.courts.models import ClubMembership
from apps.courts.services import (
    MembershipError,
    assign_membership,
    can_access_club,
    can_book_court,
    cancel_membership,
    get_active_memberships,
    get_bookable_courts,
    get_visible_clubs,
    suspend_membership,
)


class VisibilityScenarioMixin:
    """
    Two open clubs and three members-only clubs. The player belongs to two
    of the members-only clubs, so they should see 2 open + 2 private.
    """

    def setUp(self):
        super().setUp()
        self.player = create_user()

        self.open_a = create_club(name="Open A")
        self.open_b = create_club(name="Open B")
        self.private_a = create_members_only_club(name="Private A")
        self.private_b = create_members_only_club(name="Private B")
        self.private_c = create_members_only_club(name="Private C")

        create_club_membership(user=self.player, club=self.private_a)
        create_club_membership(user=self.player, club=self.private_b)

        self.courts = {
            club: create_court(club=club)
            for club in (self.open_a, self.open_b, self.private_a, self.private_b, self.private_c)
        }


class GetVisibleClubsTests(VisibilityScenarioMixin, TestCase):
    def test_member_sees_open_clubs_and_own_private_clubs(self):
        self.assertCountEqual(
            get_visible_clubs(self.player),
            [self.open_a, self.open_b, self.private_a, self.private_b],
        )

    def test_non_member_sees_only_open_clubs(self):
        self.assertCountEqual(get_visible_clubs(create_user()), [self.open_a, self.open_b])

    def test_anonymous_sees_only_open_clubs(self):
        for anonymous in (None, AnonymousUser()):
            with self.subTest(user=anonymous):
                self.assertCountEqual(get_visible_clubs(anonymous), [self.open_a, self.open_b])

    def test_inactive_clubs_are_hidden(self):
        self.private_a.is_active = False
        self.private_a.save(update_fields=["is_active"])

        self.assertNotIn(self.private_a, get_visible_clubs(self.player))


class GetBookableCourtsTests(VisibilityScenarioMixin, TestCase):
    def test_member_gets_courts_across_all_accessible_clubs(self):
        expected = [self.courts[c] for c in (self.open_a, self.open_b, self.private_a, self.private_b)]

        self.assertCountEqual(get_bookable_courts(self.player), expected)

    def test_filter_by_club(self):
        self.assertCountEqual(
            get_bookable_courts(self.player, club=self.private_a),
            [self.courts[self.private_a]],
        )

    def test_filter_by_inaccessible_club_returns_nothing(self):
        self.assertFalse(get_bookable_courts(self.player, club=self.private_c).exists())

    def test_expired_membership_hides_private_courts(self):
        ClubMembership.objects.filter(user=self.player, club=self.private_a).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        self.assertNotIn(self.courts[self.private_a], get_bookable_courts(self.player))

    def test_suspended_membership_hides_private_courts(self):
        ClubMembership.objects.filter(user=self.player, club=self.private_b).update(
            status=ClubMembership.Status.SUSPENDED
        )

        self.assertNotIn(self.courts[self.private_b], get_bookable_courts(self.player))

    def test_query_count_is_constant(self):
        """Visibility is a single SQL query (EXISTS subquery, club joined)."""
        with self.assertNumQueries(1):
            courts = list(get_bookable_courts(self.player))
            _ = [court.club.name for court in courts]


class AccessPredicateTests(TestCase):
    def setUp(self):
        self.player = create_user()
        self.open_club = create_club()
        self.private_club = create_members_only_club()

    def test_everyone_can_access_open_club(self):
        self.assertTrue(can_access_club(self.player, self.open_club))
        self.assertTrue(can_access_club(None, self.open_club))

    def test_membership_of_another_club_does_not_grant_access(self):
        create_club_membership(user=self.player, club=create_members_only_club())

        self.assertFalse(can_access_club(self.player, self.private_club))
        self.assertFalse(can_access_club(None, self.private_club))  # nor does being anonymous

    def test_can_book_court_requires_active_court_and_club(self):
        court = create_court(club=self.open_club, is_active=False)

        self.assertFalse(can_book_court(self.player, court))

    def test_can_book_private_court_only_as_member(self):
        court = create_court(club=self.private_club)
        self.assertFalse(can_book_court(self.player, court))

        create_club_membership(user=self.player, club=self.private_club)
        self.assertTrue(can_book_court(self.player, court))


class GetActiveMembershipsTests(TestCase):
    def test_returns_only_valid_memberships(self):
        player = create_user()
        valid = create_club_membership(user=player)
        create_club_membership(user=player, status=ClubMembership.Status.CANCELLED)
        create_club_membership()  # someone else's

        self.assertQuerySetEqual(get_active_memberships(player), [valid])


class AssignMembershipTests(TestCase):
    def setUp(self):
        self.player = create_user()
        self.club = create_members_only_club()

    def test_creates_active_membership(self):
        membership = assign_membership(
            user=self.player, club=self.club, tier=ClubMembership.Tier.VIP
        )

        self.assertEqual(membership.status, ClubMembership.Status.ACTIVE)
        self.assertEqual(membership.tier, ClubMembership.Tier.VIP)
        self.assertTrue(can_access_club(self.player, self.club))

    def test_assign_to_multiple_clubs(self):
        other = create_members_only_club()

        assign_membership(user=self.player, club=self.club)
        assign_membership(user=self.player, club=other)

        self.assertCountEqual(self.player.clubs.all(), [self.club, other])

    def test_is_idempotent_and_updates_existing(self):
        first = assign_membership(user=self.player, club=self.club)
        expires_at = timezone.now() + timedelta(days=365)

        second = assign_membership(
            user=self.player,
            club=self.club,
            tier=ClubMembership.Tier.PREMIUM,
            expires_at=expires_at,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ClubMembership.objects.filter(user=self.player).count(), 1)
        self.assertEqual(second.tier, ClubMembership.Tier.PREMIUM)
        self.assertEqual(second.expires_at, expires_at)

    def test_reactivates_cancelled_membership(self):
        existing = create_club_membership(
            user=self.player, club=self.club, status=ClubMembership.Status.CANCELLED
        )

        membership = assign_membership(user=self.player, club=self.club)

        self.assertEqual(membership.pk, existing.pk)
        self.assertEqual(membership.status, ClubMembership.Status.ACTIVE)

    def test_rejects_expiry_in_the_past(self):
        with self.assertRaises(MembershipError):
            assign_membership(
                user=self.player,
                club=self.club,
                expires_at=timezone.now() - timedelta(days=1),
            )
        self.assertFalse(ClubMembership.objects.exists())


class MembershipTransitionTests(TestCase):
    def test_suspend_active_membership_revokes_access(self):
        membership = create_club_membership()

        suspend_membership(membership)

        membership.refresh_from_db()
        self.assertEqual(membership.status, ClubMembership.Status.SUSPENDED)
        self.assertFalse(can_access_club(membership.user, membership.club))

    def test_cancel_from_active_or_suspended(self):
        for status in (ClubMembership.Status.ACTIVE, ClubMembership.Status.SUSPENDED):
            with self.subTest(status=status):
                membership = create_club_membership(status=status)

                cancel_membership(membership)

                membership.refresh_from_db()
                self.assertEqual(membership.status, ClubMembership.Status.CANCELLED)

    def test_invalid_transitions_raise(self):
        cancelled = create_club_membership(status=ClubMembership.Status.CANCELLED)
        suspended = create_club_membership(status=ClubMembership.Status.SUSPENDED)

        with self.assertRaises(MembershipError):
            suspend_membership(cancelled)
        with self.assertRaises(MembershipError):
            suspend_membership(suspended)
        with self.assertRaises(MembershipError):
            cancel_membership(cancelled)
