"""Tests for ClubStaffRole: a user's roles in running a club, with history."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from apps.common.tests.factories import (
    create_club,
    create_club_staff_role,
    create_superuser,
    create_user,
)
from apps.common.tests.utils import freeze_time
from apps.courts.models import ClubStaffRole

ADMIN = ClubStaffRole.Role.ADMIN
GROUNDSKEEPER = ClubStaffRole.Role.GROUNDSKEEPER
FRONT_DESK = ClubStaffRole.Role.FRONT_DESK
DAY = date(2030, 1, 15)


def constraint(name: str):
    return next(c for c in ClubStaffRole._meta.constraints if c.name == name)


class ClubStaffRoleFieldTests(TestCase):
    def test_defaults(self):
        with freeze_time(datetime(2030, 1, 15, 12, tzinfo=ZoneInfo("America/Sao_Paulo"))):
            role = create_club_staff_role()

        role.refresh_from_db()
        self.assertEqual(role.start_date, DAY)
        self.assertIsNone(role.end_date)
        self.assertTrue(role.is_active)
        self.assertEqual(role.role, FRONT_DESK)

    def test_start_date_default_uses_the_project_timezone(self):
        # 01:30 UTC on the 16th is still the 15th in São Paulo (UTC-3).
        with freeze_time(datetime(2030, 1, 16, 1, 30, tzinfo=ZoneInfo("UTC"))):
            role = create_club_staff_role()

        self.assertEqual(role.start_date, DAY)

    def test_role_check_constraint_matches_the_choices(self):
        check = constraint("club_staff_role_valid_role")

        self.assertEqual(set(check.condition.children[0][1]), set(ClubStaffRole.Role.values))

    def test_unknown_roles_are_rejected_by_the_database(self):
        for role in ("MEMBER", "STAFF"):  # create() skips choice validation
            with self.subTest(role), self.assertRaises(IntegrityError), transaction.atomic():
                create_club_staff_role(role=role)

class ClubStaffRoleActiveUniquenessTests(TestCase):
    """At most one *active* record per (user, club, role); inactive ones are history."""

    def setUp(self):
        self.user = create_user()
        self.club = create_club()

    def grant(self, role=FRONT_DESK, **extra):
        return create_club_staff_role(user=self.user, club=self.club, role=role, **extra)

    def test_two_active_records_of_the_same_role_are_rejected(self):
        self.grant()

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.grant()

    def test_history_can_hold_many_inactive_records(self):
        for months_ago in (3, 2, 1):
            self.grant(start_date=DAY - timedelta(days=30 * months_ago), is_active=False)
        self.grant(start_date=DAY)

        self.assertEqual(self.user.club_staff_roles.filter(role=FRONT_DESK).count(), 4)
        self.assertEqual(self.user.club_staff_roles.filter(role=FRONT_DESK, is_active=True).count(), 1)

    def test_reactivating_an_old_record_is_rejected_while_another_is_active(self):
        old = self.grant(is_active=False)
        self.grant()

        old.is_active = True
        with self.assertRaises(IntegrityError), transaction.atomic():
            old.save()

    def test_a_record_ended_by_date_but_still_active_blocks_a_new_one(self):
        # The rule is on is_active only: an ended record must be deactivated first.
        self.grant(start_date=DAY - timedelta(days=90), end_date=DAY - timedelta(days=1))

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.grant(start_date=DAY)

    def test_all_roles_can_be_active_at_the_same_time(self):
        for role in (ADMIN, GROUNDSKEEPER, FRONT_DESK):
            self.grant(role, start_date=DAY)

        self.assertCountEqual(
            ClubStaffRole.objects.current(on=DAY).filter(user=self.user, club=self.club)
            .values_list("role", flat=True),
            [ADMIN, GROUNDSKEEPER, FRONT_DESK],
        )

    def test_same_active_role_at_different_clubs_is_allowed(self):
        self.grant(ADMIN)
        create_club_staff_role(user=self.user, club=create_club(), role=ADMIN)

        self.assertEqual(self.user.club_staff_roles.filter(role=ADMIN, is_active=True).count(), 2)

    def test_same_active_role_for_different_users_is_allowed(self):
        self.grant(ADMIN)
        create_club_staff_role(user=create_user(), club=self.club, role=ADMIN)

        self.assertEqual(self.club.staff_roles.filter(role=ADMIN, is_active=True).count(), 2)


class ClubStaffRoleDateTests(TestCase):
    def test_end_before_start_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_club_staff_role(start_date=DAY, end_date=DAY - timedelta(days=1))

    def test_single_day_role_is_allowed(self):
        create_club_staff_role(start_date=DAY, end_date=DAY).full_clean()  # does not raise


class ClubStaffRoleCurrentTests(TestCase):
    """`current(on)` / `is_current(on)`: is_active, started, and end_date not passed (inclusive)."""

    def setUp(self):
        club = create_club()
        cases = {
            "open_ended": dict(start_date=DAY - timedelta(days=10)),
            "ends_today": dict(start_date=DAY - timedelta(days=10), end_date=DAY),
            "starts_today": dict(start_date=DAY),
            "ended_yesterday": dict(start_date=DAY - timedelta(days=10), end_date=DAY - timedelta(days=1)),
            "starts_tomorrow": dict(start_date=DAY + timedelta(days=1)),
            "inactive": dict(start_date=DAY - timedelta(days=10), is_active=False),
        }
        self.roles = {name: create_club_staff_role(club=club, **fields) for name, fields in cases.items()}

    def test_current_on_a_date(self):
        self.assertCountEqual(
            ClubStaffRole.objects.current(on=DAY),
            [self.roles["open_ended"], self.roles["ends_today"], self.roles["starts_today"]],
        )

    def test_is_current_agrees_with_current(self):
        current = set(ClubStaffRole.objects.current(on=DAY))

        for name, role in self.roles.items():
            with self.subTest(name):
                self.assertEqual(role.is_current(on=DAY), role in current)

    def test_defaults_to_today(self):
        with freeze_time(datetime(2030, 1, 16, 12, tzinfo=ZoneInfo("America/Sao_Paulo"))):
            current = set(ClubStaffRole.objects.current())
            ends_today_still_current = self.roles["ends_today"].is_current()

        self.assertNotIn(self.roles["ends_today"], current)  # its last day was the 15th
        self.assertIn(self.roles["starts_tomorrow"], current)
        self.assertFalse(ends_today_still_current)


class ClubStaffRoleDeletionTests(TestCase):
    def test_deleting_the_user_deletes_their_roles_and_history(self):
        user = create_user()
        create_club_staff_role(user=user, is_active=False)
        create_club_staff_role(user=user)

        user.delete()

        self.assertFalse(ClubStaffRole.objects.exists())

class ClubStaffRoleAdminTests(TestCase):
    def setUp(self):
        self.client.force_login(create_superuser())
        self.user, self.club = create_user(), create_club()

    def post_add(self, **overrides):
        data = {
            "user": str(self.user.pk), "club": str(self.club.pk), "role": FRONT_DESK,
            "start_date": "2030-01-15", "end_date": "", "is_active": "on",
        }
        data.update(overrides)
        data = {key: value for key, value in data.items() if value is not None}  # None = unticked
        return self.client.post(reverse("admin:courts_clubstaffrole_add"), data)

    def test_duplicate_active_record_is_rejected_with_a_message(self):
        self.assertEqual(self.post_add().status_code, 302)  # created

        response = self.post_add()

        self.assertContains(response, "This user already has an active record of this role at this club.")
        self.assertEqual(ClubStaffRole.objects.count(), 1)

    def test_inactive_record_is_accepted_as_history(self):
        self.post_add()

        response = self.post_add(start_date="2029-01-01", end_date="2029-12-31", is_active=None)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ClubStaffRole.objects.filter(is_active=False).count(), 1)

    def test_offers_exactly_the_three_roles(self):
        response = self.client.get(reverse("admin:courts_clubstaffrole_add"))

        offered = re.findall(r'<option value="([A-Z_]+)"', response.content.decode())
        self.assertEqual(offered, ["ADMIN", "GROUNDSKEEPER", "FRONT_DESK"])

    def test_end_before_start_via_admin(self):
        response = self.post_add(end_date="2030-01-14")

        self.assertContains(response, "The end date can&#x27;t be before the start date.")
        self.assertFalse(ClubStaffRole.objects.exists())

    def test_club_page_shows_the_roles_inline(self):
        create_club_staff_role(user=self.user, club=self.club, role=ADMIN)

        response = self.client.get(reverse("admin:courts_club_change", args=[self.club.pk]))

        self.assertContains(response, 'name="staff_roles-0-role"')
        self.assertContains(response, f"{self.user.email} @ {self.club.name} [ADMIN]")


class ClubStaffRoleClubInlineTests(TestCase):
    """Editing roles from the club's admin page (inline formset)."""

    def setUp(self):
        self.client.force_login(create_superuser())
        self.user, self.club = create_user(), create_club(name="Ace", address="Rua X")
        self.old = create_club_staff_role(user=self.user, club=self.club, start_date=date(2029, 1, 1))

    def row(self, i, *, pk=None, user=None, role=FRONT_DESK, start="2030-01-01", end="", active=True):
        data = {
            f"staff_roles-{i}-club": str(self.club.pk),
            f"staff_roles-{i}-user": str((user or self.user).pk),
            f"staff_roles-{i}-role": role,
            f"staff_roles-{i}-start_date": start,
            f"staff_roles-{i}-end_date": end,
        }
        if pk:
            data[f"staff_roles-{i}-id"] = str(pk)
        if active:
            data[f"staff_roles-{i}-is_active"] = "on"
        return data

    def post(self, *rows, initial=1):
        data = {
            "name": self.club.name, "slug": self.club.slug, "timezone": self.club.timezone,
            "address": self.club.address, "cancellation_window_hours": "24", "is_active": "on",
            "access_type": "OPEN",
            "courts-TOTAL_FORMS": "0", "courts-INITIAL_FORMS": "0",
            "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
            "staff_roles-TOTAL_FORMS": str(len(rows)), "staff_roles-INITIAL_FORMS": str(initial),
        }
        for row in rows:
            data.update(row)
        return self.client.post(reverse("admin:courts_club_change", args=[self.club.pk]), data)

    def state(self):
        return list(ClubStaffRole.objects.order_by("start_date").values_list("start_date", "is_active"))

    def test_deactivate_and_add_the_successor_in_one_save(self):
        response = self.post(
            self.row(0, pk=self.old.pk, start="2029-01-01", end="2029-12-31", active=False),
            self.row(1),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state(), [(date(2029, 1, 1), False), (date(2030, 1, 1), True)])

    def test_duplicate_active_record_is_a_form_error_not_a_server_error(self):
        response = self.post(self.row(0, pk=self.old.pk, start="2029-01-01"), self.row(1))

        self.assertContains(response, "This user already has an active record of this role at this club.")
        self.assertEqual(self.state(), [(date(2029, 1, 1), True)])

    def test_two_new_active_duplicates_are_a_form_error(self):
        self.old.delete()

        response = self.post(self.row(0), self.row(1, start="2030-02-01"), initial=0)

        self.assertContains(response, "This user already has an active record of this role at this club.")
        self.assertFalse(ClubStaffRole.objects.exists())

    def test_delete_and_replace_in_one_save(self):
        old_row = self.row(0, pk=self.old.pk, start="2029-01-01")
        old_row["staff_roles-0-DELETE"] = "on"

        response = self.post(old_row, self.row(1))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state(), [(date(2030, 1, 1), True)])

    def test_other_users_and_roles_are_independent(self):
        response = self.post(
            self.row(0, pk=self.old.pk, start="2029-01-01"),
            self.row(1, role=ADMIN),
            self.row(2, user=create_user()),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ClubStaffRole.objects.filter(is_active=True).count(), 3)
