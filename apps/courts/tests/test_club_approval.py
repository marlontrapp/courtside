"""Self-service club registration with approval: pending clubs, their admins, the staff review."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings.services import BookingError, create_reservation
from apps.common.tests.factories import add_full_week_slots, create_club, create_court, create_superuser, create_user
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import Club, ClubStaffRole
from apps.courts.search import search_clubs
from apps.courts.services import get_bookable_courts, get_visible_clubs

SP = ZoneInfo("America/Sao_Paulo")
REGISTER = reverse("courts:club-register")
PENDING = reverse("courts:club-pending")


def detail(club_id) -> str:
    return reverse("courts:club-detail", args=[club_id])


class ApprovalTestCase(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 9, 0, tzinfo=SP)

    def setUp(self):
        super().setUp()
        self.player = create_user(email="owner@example.com", first_name="Olga", last_name="Owner")
        self.other = create_user()
        self.staff = create_user(email="staff@example.com", is_staff=True)

    def register(self, user=None, name="Zq Tennis Club", address="Rua Zq, 1"):
        self.client.force_authenticate(user or self.player)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(REGISTER, {"name": name, "address": address}, format="json")

    def review(self, action, club_id, body=None, user=None):
        self.client.force_authenticate(user or self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse(f"courts:club-{action}", args=[club_id]), body or {}, format="json")


class RegistrationTests(ApprovalTestCase):
    @override_settings(CLUB_REVIEW_NOTIFY_EMAILS=["reviews@example.com"])
    def test_a_player_registers_a_pending_club_and_becomes_its_admin(self):
        response = self.register()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual((response.data["approval_status"], response.data["rejection_reason"]), ("PENDING", ""))
        club = Club.objects.get()
        self.assertEqual((club.registered_by, club.reviewed_by, club.reviewed_at), (self.player, None, None))
        self.assertTrue(ClubStaffRole.objects.current().filter(
            user=self.player, club=club, role=ClubStaffRole.Role.ADMIN).exists())
        self.assertEqual(mail.outbox[-1].to, ["reviews@example.com"])
        self.assertIn("Zq Tennis Club", mail.outbox[-1].subject)
        self.assertIn(str(club.pk), mail.outbox[-1].body)

    def test_no_reviewers_configured_no_email(self):
        self.register()

        self.assertEqual(mail.outbox, [])

    def test_staff_register_approved_clubs(self):
        response = self.register(self.staff)

        self.assertEqual(response.data["approval_status"], "APPROVED")
        club = Club.objects.get()
        self.assertEqual(club.reviewed_by, self.staff)
        self.assertFalse(ClubStaffRole.objects.filter(club=club).exists())

    def test_approval_fields_cant_be_sent(self):
        self.client.force_authenticate(self.player)

        self.client.post(REGISTER, {"name": "Zq", "address": "Rua Zq", "approval_status": "APPROVED",
                                    "registered_by": str(self.other.pk)}, format="json")

        self.assertEqual((Club.objects.get().approval_status, Club.objects.get().registered_by),
                         ("PENDING", self.player))

    def test_clubs_from_staff_tools_are_approved(self):
        self.assertEqual(create_club().approval_status, "APPROVED")  # the admin, the seed, factories

    @override_settings(CLUB_REGISTRATION_MAX_PENDING=2)
    def test_pending_clubs_per_user_are_limited(self):
        first = self.register(name="One").data["id"]
        self.register(name="Two")

        refused = self.register(name="Three")

        self.assertEqual(refused.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("2 clubs awaiting approval", refused.data["detail"])
        self.assertEqual(self.register(self.other, name="Other's").status_code, status.HTTP_201_CREATED)
        self.review("approve", first)  # reviewed clubs don't count
        self.assertEqual(self.register(name="Three").status_code, status.HTTP_201_CREATED)
        for name in ("Four", "Five", "Six"):
            self.assertEqual(self.register(self.staff, name=name).status_code, status.HTTP_201_CREATED)  # staff: no limit


class PendingClubsAreHiddenTests(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.club_id = self.register().data["id"]
        self.club = Club.objects.get(pk=self.club_id)
        self.court = create_court(club=self.club, name="Zq Court")
        add_full_week_slots(self.court, open_time=time(8), close_time=time(22))

    def test_from_players(self):
        self.assertEqual(list(search_clubs("zq tennis")), [])
        self.assertNotIn(self.club, get_visible_clubs(self.other))
        self.assertNotIn(self.court, get_bookable_courts(self.other))
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(detail(self.club_id)).status_code, status.HTTP_404_NOT_FOUND)
        courts = self.client.get(reverse("courts:court-list")).data["results"]
        self.assertNotIn(str(self.court.pk), [c["id"] for c in courts])

    def test_not_bookable(self):
        start = datetime(2030, 1, 8, 10, tzinfo=SP)
        with self.assertRaisesMessage(BookingError, "awaiting approval"):
            create_reservation(court=self.court, primary_player=self.other, start_datetime=start,
                               end_datetime=start + timedelta(hours=1))
        self.client.force_authenticate(self.other)
        response = self.client.post(reverse("bookings:reservation-list"), {
            "court": str(self.court.pk), "date": "2030-01-08", "start_time": "10:00", "end_time": "11:00"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)  # the court "doesn't exist"

    def test_its_admin_and_staff_see_it(self):
        for user in (self.player, self.staff):
            self.client.force_authenticate(user)
            with self.subTest(user.email):
                self.assertEqual(self.client.get(detail(self.club_id)).data["approval_status"], "PENDING")

    def test_its_admin_sets_it_up_meanwhile(self):
        self.client.force_authenticate(self.player)

        edited = self.client.patch(detail(self.club_id), {"timezone": "Europe/Lisbon"}, format="json")
        template = self.client.post(reverse("courts:day-template-list", args=[self.club_id]),
                                    {"name": "Mornings", "slots": [{"start": "08:00", "end": "09:00"}]}, format="json")
        rule = self.client.post(reverse("bookings:booking-rule-list", args=[self.club_id]),
                                {"name": "One", "kind": "max_active_bookings", "params": {"max": 1}}, format="json")

        self.assertEqual([edited.status_code, template.status_code, rule.status_code], [200, 201, 201])

    def test_others_cant_edit_it(self):
        self.client.force_authenticate(self.other)

        self.assertEqual(self.client.patch(detail(self.club_id), {"timezone": "Europe/Lisbon"}, format="json")
                         .status_code, status.HTTP_404_NOT_FOUND)

    def test_visible_and_bookable_once_approved(self):
        self.review("approve", self.club_id)

        self.assertEqual(list(search_clubs("zq tennis")), [self.club])
        self.assertIn(self.court, get_bookable_courts(self.other))
        court = get_bookable_courts(self.other).get(pk=self.court.pk)  # loaded fresh, like a booking request
        start = datetime(2030, 1, 8, 10, tzinfo=SP)
        create_reservation(court=court, primary_player=self.other, start_datetime=start,
                           end_datetime=start + timedelta(hours=1))
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(detail(self.club_id)).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.patch(detail(self.club_id), {"timezone": "Europe/Lisbon"}, format="json")
                         .status_code, status.HTTP_403_FORBIDDEN)  # visible, but not theirs to edit


class ReviewTests(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.club_id = self.register().data["id"]
        mail.outbox.clear()

    def test_approve(self):
        response = self.review("approve", self.club_id)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["approval_status"], "APPROVED")
        club = Club.objects.get(pk=self.club_id)
        self.assertEqual((club.reviewed_by, club.reviewed_at), (self.staff, self.frozen_now))
        self.assertEqual((mail.outbox[-1].to, mail.outbox[-1].subject),
                         (["owner@example.com"], "Zq Tennis Club is approved"))
        self.assertEqual(self.review("approve", self.club_id).status_code, status.HTTP_409_CONFLICT)

    def test_reject_with_a_reason(self):
        response = self.review("reject", self.club_id, {"reason": "  Address not found  "})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data["approval_status"], response.data["rejection_reason"]),
                         ("REJECTED", "Address not found"))
        self.assertIn("Reason: Address not found", mail.outbox[-1].body)
        self.client.force_authenticate(self.player)
        self.assertEqual(self.client.get(detail(self.club_id)).data["rejection_reason"], "Address not found")
        self.assertEqual(list(search_clubs("zq tennis")), [])

    def test_a_reason_is_required(self):
        for body in ({}, {"reason": "   "}):
            with self.subTest(body):
                self.assertEqual(self.review("reject", self.club_id, body).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Club.objects.get(pk=self.club_id).approval_status, "PENDING")

    def test_transitions(self):
        self.review("reject", self.club_id, {"reason": "No"})
        self.assertEqual(self.review("reject", self.club_id, {"reason": "Again"}).status_code, status.HTTP_409_CONFLICT)

        self.assertEqual(self.review("approve", self.club_id).status_code, status.HTTP_200_OK)  # changed their mind
        self.assertEqual(self.review("reject", self.club_id, {"reason": "x"}).status_code, status.HTTP_409_CONFLICT)

    def test_staff_only(self):
        for user in (self.player, self.other):
            with self.subTest(user.email):
                self.assertEqual(self.review("approve", self.club_id, user=user).status_code, status.HTTP_403_FORBIDDEN)
                self.assertEqual(self.review("reject", self.club_id, {"reason": "x"}, user=user).status_code,
                                 status.HTTP_403_FORBIDDEN)
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(PENDING).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Club.objects.get(pk=self.club_id).approval_status, "PENDING")

    def test_pending_queue(self):
        Club.objects.filter(pk=self.club_id).update(created_at=self.frozen_now - timedelta(hours=1))
        newer = self.register(self.other, name="Newer").data["id"]
        create_club(name="Already approved")
        rejected = self.register(create_user(), name="Rejected").data["id"]
        self.review("reject", rejected, {"reason": "No"})
        self.client.force_authenticate(self.staff)

        response = self.client.get(PENDING)

        self.assertEqual([c["id"] for c in response.data["results"]], [self.club_id, newer])  # oldest first
        self.assertEqual(response.data["results"][0]["registered_by"],
                         {"id": str(self.player.pk), "first_name": "Olga", "last_name": "Owner"})
        self.assertNotIn("owner@example.com", response.content.decode())


class AdminReviewTests(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.club_id = self.register().data["id"]
        mail.outbox.clear()
        self.client.force_login(create_superuser())

    def change(self, **fields):
        club = Club.objects.get(pk=self.club_id)
        data = {"name": club.name, "slug": club.slug, "timezone": club.timezone, "address": club.address,
                "cancellation_window_hours": "24", "is_active": "on", "access_type": "OPEN",
                "approval_status": club.approval_status, "rejection_reason": "",
                "courts-TOTAL_FORMS": "0", "courts-INITIAL_FORMS": "0",
                "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
                "staff_roles-TOTAL_FORMS": "0", "staff_roles-INITIAL_FORMS": "0", **fields}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("admin:courts_club_change", args=[self.club_id]), data)

    def test_approve_action(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:courts_club_changelist"),
                             {"action": "approve", "_selected_action": [self.club_id]})

        self.assertEqual(Club.objects.get(pk=self.club_id).approval_status, "APPROVED")
        self.assertEqual(mail.outbox[-1].to, ["owner@example.com"])

    def test_reject_in_the_form_needs_a_reason_and_emails_it(self):
        refused = self.change(approval_status="REJECTED")
        self.assertContains(refused, "Give the reason")
        self.assertEqual(Club.objects.get(pk=self.club_id).approval_status, "PENDING")

        self.change(approval_status="REJECTED", rejection_reason="Duplicate", timezone="Europe/Lisbon")

        club = Club.objects.get(pk=self.club_id)
        self.assertEqual((club.approval_status, club.rejection_reason, club.timezone),
                         ("REJECTED", "Duplicate", "Europe/Lisbon"))
        self.assertIsNotNone(club.reviewed_by)
        self.assertIn("Reason: Duplicate", mail.outbox[-1].body)

    def test_cant_go_back_to_pending(self):
        self.change(approval_status="APPROVED")

        refused = self.change(approval_status="PENDING")

        self.assertContains(refused, "can&#x27;t go back")
        self.assertEqual(Club.objects.get(pk=self.club_id).approval_status, "APPROVED")
