"""Managing a club's courts: /api/v1/clubs/<club>/courts/ — club admins (self-service) and site staff."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings.models import BookingRule, Reservation
from apps.common.tests.factories import (
    add_full_week_slots,
    create_booking_rule,
    create_club,
    create_club_staff_role,
    create_court,
    create_reservation,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import ClubStaffRole, Court

SP = ZoneInfo("America/Sao_Paulo")
TODAY = datetime(2030, 1, 7, tzinfo=SP).date()


def at(day_offset: int, hour: int) -> datetime:
    return datetime.combine(TODAY + timedelta(days=day_offset), time(hour), tzinfo=SP)


class ClubCourtsTestCase(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 9, 30, tzinfo=SP)

    def setUp(self):
        super().setUp()
        self.club = create_club(name="Court Club")
        self.admin = create_user()
        create_club_staff_role(user=self.admin, club=self.club, role=ClubStaffRole.Role.ADMIN, start_date=TODAY)
        self.client.force_authenticate(self.admin)
        self.url = reverse("courts:club-court-list", args=[self.club.pk])

    def detail(self, court) -> str:
        return reverse("courts:club-court-detail", args=[self.club.pk, court.pk])


class CreateAndListTests(ClubCourtsTestCase):
    def test_add_a_court(self):
        response = self.client.post(self.url, {
            "name": "  Court 1  ", "surface_type": "CLAY", "is_singles_only": True,
            "hourly_rate": "120.00", "lighting_fee": "30.00",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        court = Court.objects.get()
        self.assertEqual(response.data, {
            "id": str(court.pk), "name": "Court 1", "surface_type": "CLAY", "is_indoor": False,
            "is_singles_only": True, "has_lighting": True, "hourly_rate": "120.00", "lighting_fee": "30.00",
            "is_active": True, "created_at": response.data["created_at"], "updated_at": response.data["updated_at"],
        })
        self.assertEqual((court.club, court.hourly_rate), (self.club, Decimal("120.00")))

    def test_names_are_unique_within_the_club(self):
        create_court(club=self.club, name="Court 1")
        create_court(club=create_club(), name="Court 2")

        taken = self.client.post(self.url, {"name": "Court 1"}, format="json")
        elsewhere = self.client.post(self.url, {"name": "Court 2"}, format="json")

        self.assertEqual(taken.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already has a court with this name", str(taken.data["name"]))
        self.assertEqual(elsewhere.status_code, status.HTTP_201_CREATED)

    def test_invalid(self):
        for body, field in (({}, "name"), ({"name": "X", "hourly_rate": "-1"}, "hourly_rate"),
                            ({"name": "X", "surface_type": "ICE"}, "surface_type")):
            with self.subTest(body):
                response = self.client.post(self.url, body, format="json")

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)

    def test_list_includes_inactive_courts_only_of_this_club(self):
        active, inactive = create_court(club=self.club, name="A"), create_court(club=self.club, name="B", is_active=False)
        create_court(club=create_club(), name="Elsewhere")

        response = self.client.get(self.url)

        self.assertEqual([c["id"] for c in response.data], [str(active.pk), str(inactive.pk)])

    def test_a_pending_club_can_be_set_up(self):
        player = create_user()
        self.client.force_authenticate(player)
        club_id = self.client.post(reverse("courts:club-register"), {"name": "Mine", "address": "Rua"},
                                   format="json").data["id"]

        response = self.client.post(reverse("courts:club-court-list", args=[club_id]), {"name": "C1"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class PermissionTests(ClubCourtsTestCase):
    def test_only_club_admins_and_staff(self):
        court = create_court(club=self.club)
        for user in (create_user(), create_club_staff_role(club=self.club, role=ClubStaffRole.Role.FRONT_DESK).user):
            self.client.force_authenticate(user)
            with self.subTest(user.email):
                self.assertEqual(self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(self.client.post(self.url, {"name": "X"}, format="json").status_code,
                                 status.HTTP_404_NOT_FOUND)
                self.assertEqual(self.client.patch(self.detail(court), {"name": "Y"}, format="json").status_code,
                                 status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(create_user(is_staff=True))
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_another_clubs_court_through_this_club_is_not_found(self):
        foreign = create_court(club=create_club())

        self.assertEqual(self.client.get(reverse("courts:club-court-detail", args=[self.club.pk, foreign.pk]))
                         .status_code, status.HTTP_404_NOT_FOUND)


class EditTests(ClubCourtsTestCase):
    def setUp(self):
        super().setUp()
        self.court = create_court(club=self.club, name="Court 1", hourly_rate=Decimal("100.00"))
        add_full_week_slots(self.court, open_time=time(8), close_time=time(22))

    def test_edit_settings_existing_bookings_keep_their_price(self):
        booking = create_reservation(court=self.court, start_datetime=at(1, 10), end_datetime=at(1, 11),
                                     total_price=Decimal("100.00"))

        response = self.client.patch(self.detail(self.court), {"hourly_rate": "150.00", "is_singles_only": True,
                                                                "name": "Court One"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data["name"], response.data["hourly_rate"], response.data["cancelled_reservations"]),
                         ("Court One", "150.00", []))
        booking.refresh_from_db()
        self.assertEqual((booking.total_price, booking.status), (Decimal("100.00"), "CONFIRMED"))

    def test_deactivating_with_future_bookings_needs_force(self):
        future = create_reservation(court=self.court, start_datetime=at(1, 10), end_datetime=at(1, 11))
        past = create_reservation(court=self.court, start_datetime=at(-1, 10), end_datetime=at(-1, 11))

        refused = self.client.patch(self.detail(self.court), {"is_active": False}, format="json")

        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual([r["id"] for r in refused.data["conflicting_reservations"]], [str(future.pk)])
        self.assertIn("force=true", refused.data["hint"])
        self.assertTrue(Court.objects.get(pk=self.court.pk).is_active)

        forced = self.client.patch(self.detail(self.court), {"is_active": False, "force": True}, format="json")

        self.assertEqual(forced.status_code, status.HTTP_200_OK, forced.data)
        self.assertFalse(forced.data["is_active"])
        self.assertEqual([r["id"] for r in forced.data["cancelled_reservations"]], [str(future.pk)])
        self.assertEqual(Reservation.objects.get(pk=future.pk).status, "CANCELLED")
        self.assertEqual(Reservation.objects.get(pk=past.pk).status, "CONFIRMED")  # history untouched

    def test_deactivating_without_future_bookings(self):
        response = self.client.patch(self.detail(self.court), {"is_active": False}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_rename_to_a_taken_name(self):
        create_court(club=self.club, name="Court 2")

        response = self.client.patch(self.detail(self.court), {"name": "Court 2"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.patch(self.detail(self.court), {"name": "Court 1"}, format="json").status_code,
                         status.HTTP_200_OK)  # its own name is fine


class DeleteTests(ClubCourtsTestCase):
    def setUp(self):
        super().setUp()
        self.court = create_court(club=self.club, name="Court 1")

    def test_a_court_without_bookings(self):
        self.assertEqual(self.client.delete(self.detail(self.court)).status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Court.objects.exists())

    def test_a_court_with_bookings_even_cancelled_ones(self):
        create_reservation(court=self.court, status=Reservation.Status.CANCELLED)

        response = self.client.delete(self.detail(self.court) + "?force=true")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("deactivate it instead", response.data["detail"])
        self.assertTrue(Court.objects.filter(pk=self.court.pk).exists())

    def test_linked_to_booking_rules(self):
        rule = create_booking_rule(self.club, courts=[self.court], name="Court 1 only")

        refused = self.client.delete(self.detail(self.court))
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(refused.data["booking_rules"], [{"id": str(rule.pk), "name": "Court 1 only"}])

        forced = self.client.delete(self.detail(self.court) + "?force=true")
        self.assertEqual(forced.status_code, status.HTTP_200_OK)
        self.assertEqual([r["name"] for r in forced.data["deactivated_booking_rules"]], ["Court 1 only"])
        self.assertFalse(BookingRule.objects.get(pk=rule.pk).is_active)


class FullSelfServiceTests(FrozenTimeMixin, APITestCase):
    """A player sets up a whole club through the API; once staff approve it, others book it."""

    frozen_now = datetime(2030, 1, 7, 9, 30, tzinfo=SP)

    def test_register_set_up_approve_book(self):
        owner, staff, player = create_user(), create_user(is_staff=True), create_user()
        self.client.force_authenticate(owner)
        club = self.client.post(reverse("courts:club-register"), {"name": "Zq Self Club", "address": "Rua Zq"},
                                format="json").data
        court = self.client.post(reverse("courts:club-court-list", args=[club["id"]]),
                                 {"name": "Center", "hourly_rate": "80.00"}, format="json").data
        template = self.client.post(reverse("courts:day-template-list", args=[club["id"]]), {
            "name": "Days", "slots": [{"start": "08:00", "end": "09:00"}, {"start": "09:00", "end": "10:00"}]},
            format="json").data
        schedule = self.client.post(reverse("courts:court-schedule-list", args=[court["id"]]), {
            "valid_from": TODAY.isoformat(),
            "days": [{"weekdays": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"],
                      "day_template": template["id"]}]}, format="json")
        self.assertEqual(schedule.status_code, status.HTTP_201_CREATED, schedule.data)

        self.client.force_authenticate(player)
        self.assertEqual(self.client.get(reverse("courts:court-list"), {"date": "2030-01-08"}).data["count"], 0)

        self.client.force_authenticate(staff)
        self.assertEqual(self.client.post(reverse("courts:club-approve", args=[club["id"]])).status_code, 200)

        self.client.force_authenticate(player)
        listed = self.client.get(reverse("courts:court-list"), {"date": "2030-01-08"}).data["results"]
        self.assertEqual([c["id"] for c in listed], [court["id"]])
        booked = self.client.post(reverse("bookings:reservation-list"), {
            "court": court["id"], "date": "2030-01-08", "start_time": "08:00", "end_time": "10:00"}, format="json")
        self.assertEqual(booked.status_code, status.HTTP_201_CREATED, booked.data)
        self.assertEqual(booked.data["total_price"], "160.00")
