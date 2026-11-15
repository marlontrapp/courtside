"""
Deleting courts / day templates / slots linked to booking rules: refused
(409, listing the rules) unless forced; forcing unlinks them, and a rule left
without any link of a kind it had is deactivated rather than widened.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings.models import BookingRule, Reservation
from apps.bookings.rules import unlink_from_rules
from apps.common.tests.factories import (
    create_booking_rule,
    create_club,
    create_club_staff_role,
    create_court,
    create_court_schedule,
    create_day_template,
    create_reservation,
    create_superuser,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import ClubStaffRole, Court, DayTemplate
from apps.courts.schedule import grid_slots
from apps.courts.services import LinkedToRules, delete_court

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)


def reloaded(rule) -> BookingRule:
    return BookingRule.objects.prefetch_related("courts", "day_templates", "slots").get(pk=rule.pk)


class LinkedDeletionTestCase(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 9, 30, tzinfo=SP)

    def setUp(self):
        super().setUp()
        self.club = create_club()
        self.court1, self.court2 = (create_court(club=self.club, name=f"Court {n}") for n in (1, 2))
        self.spare = create_day_template(self.club, grid_slots(time(8), time(12), 60), name="Spare")
        self.other = create_day_template(self.club, grid_slots(time(8), time(12), 60), name="Other")
        self.admin = create_user()
        create_club_staff_role(user=self.admin, club=self.club, role=ClubStaffRole.Role.ADMIN, start_date=TODAY)
        self.client.force_authenticate(self.admin)

    def slot(self, template, hour):
        return template.slots.get(start_time=time(hour))

    def rule(self, name, **links):
        return create_booking_rule(self.club, "booking_types", name=name, **links)

    def template_url(self, template):
        return reverse("courts:day-template-detail", args=[self.club.pk, template.pk])


class UnlinkTests(LinkedDeletionTestCase):
    def test_a_rule_keeping_other_links_stays_active(self):
        rule = self.rule("Two courts", courts=[self.court1, self.court2])

        report = unlink_from_rules(courts=[self.court1])

        self.assertEqual((report.unlinked, report.deactivated), ([rule], []))
        self.assertEqual(list(reloaded(rule).courts.all()), [self.court2])
        self.assertTrue(reloaded(rule).is_active)

    def test_a_rule_losing_its_last_link_of_a_kind_is_deactivated(self):
        # Courts 1 + slot 9:00 of Spare; losing the slot would make it cover every slot of court 1.
        rule = self.rule("Court 1 at 9", courts=[self.court1], slots=[self.slot(self.spare, 9)])

        report = unlink_from_rules(slots=[self.slot(self.spare, 9)])

        self.assertEqual([r.pk for r in report.deactivated], [rule.pk])
        rule = reloaded(rule)
        self.assertFalse(rule.is_active)
        self.assertEqual((list(rule.courts.all()), list(rule.slots.all())), ([self.court1], []))

    def test_unrelated_rules_are_untouched(self):
        other = self.rule("Other", courts=[self.court2])

        report = unlink_from_rules(courts=[self.court1])

        self.assertEqual((report.unlinked, report.deactivated), ([], []))
        self.assertTrue(reloaded(other).is_active)


class DeleteTemplateAPITests(LinkedDeletionTestCase):
    def test_refused_without_force(self):
        rule = self.rule("Spare mornings", slots=[self.slot(self.spare, 9)])

        response = self.client.delete(self.template_url(self.spare))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["booking_rules"], [{"id": str(rule.pk), "name": "Spare mornings"}])
        self.assertIn("force=true", response.data["hint"])
        self.assertTrue(DayTemplate.objects.filter(pk=self.spare.pk).exists())

    def test_forced(self):
        only_spare = self.rule("Spare only", day_templates=[self.spare])
        both = self.rule("Spare or other", day_templates=[self.spare, self.other])
        spare_slot = self.rule("Spare 9:00 on court 1", courts=[self.court1], slots=[self.slot(self.spare, 9)])
        untouched = self.rule("Court 2", courts=[self.court2])

        response = self.client.delete(self.template_url(self.spare) + "?force=true")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(DayTemplate.objects.filter(pk=self.spare.pk).exists())
        self.assertEqual({r["name"] for r in response.data["unlinked_booking_rules"]},
                         {"Spare only", "Spare or other", "Spare 9:00 on court 1"})
        self.assertEqual({r["name"] for r in response.data["deactivated_booking_rules"]},
                         {"Spare only", "Spare 9:00 on court 1"})
        self.assertEqual(
            {r.name: r.is_active for r in BookingRule.objects.all()},
            {only_spare.name: False, both.name: True, spare_slot.name: False, untouched.name: True},
        )
        self.assertEqual(list(reloaded(both).day_templates.all()), [self.other])

    def test_force_without_links_reports_nothing(self):
        response = self.client.delete(self.template_url(self.spare) + "?force=true")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"unlinked_booking_rules": [], "deactivated_booking_rules": []})

    def test_unforced_unlinked_delete_is_still_a_204(self):
        self.assertEqual(self.client.delete(self.template_url(self.spare)).status_code, status.HTTP_204_NO_CONTENT)

    def test_still_refused_when_used_by_a_schedule(self):
        create_court_schedule(self.court1, days={0: self.spare})
        self.rule("Spare", day_templates=[self.spare])

        response = self.client.delete(self.template_url(self.spare) + "?force=true")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)  # force only covers booking rules
        self.assertTrue(BookingRule.objects.get().is_active)


class RemoveSlotsAPITests(LinkedDeletionTestCase):
    KEEP_8_AND_10 = [{"start": "08:00", "end": "09:00"}, {"start": "10:00", "end": "11:00"}]

    def test_refused_without_force(self):
        rule = self.rule("Spare 9:00", slots=[self.slot(self.spare, 9)])

        response = self.client.patch(self.template_url(self.spare), {"slots": self.KEEP_8_AND_10}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["booking_rules"], [{"id": str(rule.pk), "name": "Spare 9:00"}])

    def test_forced(self):
        rule = self.rule("Spare 9:00", slots=[self.slot(self.spare, 9)])
        keeps = self.rule("Spare 9 or 10", slots=[self.slot(self.spare, 9), self.slot(self.spare, 10)])

        response = self.client.patch(
            self.template_url(self.spare), {"slots": self.KEEP_8_AND_10, "force": True}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual([s["start"] for s in response.data["slots"]], ["08:00", "10:00"])
        self.assertEqual(response.data["cancelled_reservations"], [])
        self.assertEqual([r["name"] for r in response.data["deactivated_booking_rules"]], ["Spare 9:00"])
        self.assertEqual({r["name"] for r in response.data["unlinked_booking_rules"]}, {"Spare 9:00", "Spare 9 or 10"})
        self.assertFalse(reloaded(rule).is_active)
        self.assertEqual([s.start_time for s in reloaded(keeps).slots.all()], [time(10)])


class DeleteCourtTests(LinkedDeletionTestCase):
    def test_service(self):
        rule = self.rule("Court 1", courts=[self.court1])

        with self.assertRaises(LinkedToRules) as ctx:
            delete_court(self.court1)
        self.assertEqual(ctx.exception.rules, [rule])

        report = delete_court(self.court1, force=True)

        self.assertFalse(Court.objects.filter(pk=self.court1.pk).exists())
        self.assertEqual([r.pk for r in report.deactivated], [rule.pk])
        self.assertFalse(reloaded(rule).is_active)

    def test_admin_delete_is_blocked_and_the_force_action_works(self):
        self.client.force_login(create_superuser())
        keeps = self.rule("Both courts", courts=[self.court1, self.court2])
        only = self.rule("Court 1 only", courts=[self.court1])

        blocked = self.client.post(reverse("admin:courts_court_delete", args=[self.court1.pk]), {"post": "yes"})
        self.assertTrue(Court.objects.filter(pk=self.court1.pk).exists())
        self.assertEqual(blocked.status_code, status.HTTP_200_OK)  # the page lists what prevents it

        forced = self.client.post(
            reverse("admin:courts_court_changelist"),
            {"action": "force_delete", "_selected_action": [str(self.court1.pk)]}, follow=True,
        )

        self.assertFalse(Court.objects.filter(pk=self.court1.pk).exists())
        self.assertContains(forced, "Court 1 only")  # deactivated: reported
        self.assertEqual({r.name: r.is_active for r in BookingRule.objects.all()}, {keeps.name: True, only.name: False})

    def test_admin_force_action_refuses_courts_with_reservations(self):
        self.client.force_login(create_superuser())
        rule = self.rule("Court 1", courts=[self.court1])
        create_reservation(court=self.court1)

        response = self.client.post(
            reverse("admin:courts_court_changelist"),
            {"action": "force_delete", "_selected_action": [str(self.court1.pk)]}, follow=True,
        )

        self.assertContains(response, "has reservations")
        self.assertTrue(Court.objects.filter(pk=self.court1.pk).exists())
        self.assertTrue(reloaded(rule).is_active)  # rolled back: still linked, still active
        self.assertEqual(list(reloaded(rule).courts.all()), [self.court1])
        self.assertEqual(Reservation.objects.count(), 1)
