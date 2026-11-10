"""
Schedule API: day templates, weekly schedule versions, copying, one-time
overrides, booking protection (409 / force), midnight, permissions.

Time is frozen on Monday 2030-01-07 12:00 in São Paulo.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bookings.models import Reservation
from apps.common.tests.factories import (
    create_club,
    create_club_staff_role,
    create_court,
    create_court_schedule,
    create_reservation,
    create_schedule_override,
    create_superuser,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import ClubStaffRole, CourtSchedule, DayTemplate, ScheduleOverride

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)  # Monday
WORKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
WEEKEND = ["SATURDAY", "SUNDAY"]


def d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def slot_list(*pairs: str) -> list[dict]:
    return [{"start": p.split("-")[0], "end": p.split("-")[1]} for p in pairs]


class ScheduleAPITestCase(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 12, 0, tzinfo=SP)

    def setUp(self):
        super().setUp()
        self.club = create_club()
        self.court = create_court(club=self.club, name="Court 1")
        self.admin = create_user(email="admin@club.example")
        create_club_staff_role(user=self.admin, club=self.club, role=ClubStaffRole.Role.ADMIN, start_date=TODAY)
        self.client.force_authenticate(self.admin)

    # URLs
    def templates_url(self, club=None):
        return reverse("courts:day-template-list", args=[(club or self.club).pk])

    def template_url(self, template):
        return reverse("courts:day-template-detail", args=[template.club_id, template["id"] if isinstance(template, dict) else template.pk])

    def schedules_url(self, court=None):
        return reverse("courts:court-schedule-list", args=[(court or self.court).pk])

    def override_url(self, day: str, court=None):
        return reverse("courts:schedule-override-detail", args=[(court or self.court).pk, day])

    # helpers
    def create_template(self, name, *pairs):
        response = self.client.post(self.templates_url(), {"name": name, "slots": slot_list(*pairs)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.json()

    def workdays_and_weekend(self, valid_from=None, court=None):
        workdays = self.create_template("Workdays", "07:00-08:00", "08:00-09:00", "18:00-19:00", "19:00-20:00")
        weekend = self.create_template("Weekend", "08:00-09:30", "09:30-11:00")
        response = self.client.post(self.schedules_url(court), {
            "valid_from": valid_from or d(0),
            "days": [
                {"weekdays": WORKDAYS, "day_template": workdays["id"]},
                {"weekdays": WEEKEND, "day_template": weekend["id"]},
            ],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return workdays, weekend, response.json()

    def available(self, day: str, court=None):
        self.client.force_authenticate(create_user())
        response = self.client.get(reverse("courts:court-list"), {"date": day})
        self.client.force_authenticate(self.admin)
        court_id = str((court or self.court).pk)
        found = [c for c in response.data["results"] if c["id"] == court_id]
        return [f"{s['start'][11:16]}-{s['end'][11:16]}" for s in found[0]["available_slots"]] if found else []


class DayTemplateAPITests(ScheduleAPITestCase):
    def test_create_and_list(self):
        created = self.create_template("Workdays", "07:00-08:00", "08:00-09:00")

        self.assertEqual(created["name"], "Workdays")
        self.assertEqual(created["slots"], slot_list("07:00-08:00", "08:00-09:00"))
        self.assertFalse(created["locked"])
        self.assertEqual([t["name"] for t in self.client.get(self.templates_url()).json()], ["Workdays"])

    def test_slots_are_returned_in_order(self):
        created = self.create_template("Mixed", "10:00-11:00", "07:00-08:00")

        self.assertEqual(created["slots"], slot_list("07:00-08:00", "10:00-11:00"))

    def test_24_00_ends_a_slot_at_midnight(self):
        created = self.create_template("Late", "22:00-23:00", "23:00-24:00")

        self.assertEqual(created["slots"][-1], {"start": "23:00", "end": "24:00"})
        self.assertEqual(DayTemplate.objects.get().slots.last().end_time.isoformat(), "23:59:59.999999")

    def test_invalid_templates(self):
        self.create_template("Taken", "08:00-09:00")
        cases = {
            "overlap": {"name": "A", "slots": slot_list("08:00-09:00", "08:30-09:30")},
            "end before start": {"name": "B", "slots": slot_list("09:00-08:00")},
            "start at 24:00": {"name": "C", "slots": slot_list("24:00-24:00")},
            "seconds": {"name": "D", "slots": [{"start": "08:00:30", "end": "09:00"}]},
            "duplicate name": {"name": "taken", "slots": []},
            "no name": {"slots": []},
        }
        for name, body in cases.items():
            with self.subTest(name):
                response = self.client.post(self.templates_url(), body, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(DayTemplate.objects.count(), 1)

    def test_overlap_message(self):
        response = self.client.post(
            self.templates_url(), {"name": "A", "slots": slot_list("08:00-09:00", "08:30-09:30")}, format="json"
        )

        self.assertEqual(response.json(), {"slots": ["08:00–09:00 overlaps 08:30–09:30."]})

    def test_rename_is_allowed_even_once_locked(self):
        template = self.create_template("Workdays", "08:00-09:00")
        workdays = DayTemplate.objects.get()
        create_court_schedule(create_court(club=self.club), {0: workdays}, valid_from=TODAY - timedelta(days=7))

        response = self.client.patch(self.template_url(workdays), {"name": "Weekdays"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.json()["name"], response.json()["locked"]), ("Weekdays", True))
        self.assertEqual(response.json()["slots"], template["slots"])

    def test_slots_can_change_until_the_template_is_in_effect(self):
        self.workdays_and_weekend(valid_from=d(1))  # planned from tomorrow
        workdays = DayTemplate.objects.get(name="Workdays")

        response = self.client.patch(self.template_url(workdays), {"slots": slot_list("07:00-09:00")}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        create_court_schedule(create_court(club=self.club), {0: workdays}, valid_from=TODAY - timedelta(days=7))
        response = self.client.patch(self.template_url(workdays), {"slots": slot_list("06:00-09:00")}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("history", response.json()["detail"])
        self.assertTrue(self.client.get(self.template_url(workdays)).json()["locked"])

    def test_delete(self):
        self.create_template("Unused", "08:00-09:00")
        workdays, _, _ = self.workdays_and_weekend()

        in_use = self.client.delete(self.template_url(DayTemplate.objects.get(name="Workdays")))
        unused = self.client.delete(self.template_url(DayTemplate.objects.get(name="Unused")))

        self.assertEqual(in_use.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(unused.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(set(DayTemplate.objects.values_list("name", flat=True)), {"Workdays", "Weekend"})


class CourtScheduleAPITests(ScheduleAPITestCase):
    def test_workdays_and_weekend(self):
        _, _, version = self.workdays_and_weekend()

        self.assertEqual(version["valid_from"], d(0))
        self.assertIsNone(version["valid_until"])
        self.assertEqual(version["state"], "current")
        self.assertEqual(
            [(group["weekdays"], group["day_template"]["name"]) for group in version["days"]],
            [(WORKDAYS, "Workdays"), (WEEKEND, "Weekend")],
        )
        self.assertEqual(version["cancelled_reservations"], [])
        self.assertEqual(self.available(d(1)), ["07:00-08:00", "08:00-09:00", "18:00-19:00", "19:00-20:00"])
        self.assertEqual(self.available(d(5)), ["08:00-09:30", "09:30-11:00"])  # Saturday

    def test_closed_weekdays_are_listed_with_null(self):
        workdays = self.create_template("Workdays", "08:00-09:00")

        response = self.client.post(self.schedules_url(), {
            "valid_from": d(0), "days": [{"weekdays": WORKDAYS, "day_template": workdays["id"]}],
        }, format="json")

        self.assertEqual(response.json()["days"][-1], {"weekdays": WEEKEND, "day_template": None})

    def test_history_list_with_states(self):
        self.workdays_and_weekend()
        early = self.create_template("Early", "06:00-07:00")
        create_court_schedule(self.court, {}, valid_from=TODAY - timedelta(days=30), valid_until=TODAY - timedelta(days=1))
        self.client.post(self.schedules_url(), {
            "valid_from": d(14), "days": [{"weekdays": ["MONDAY"], "day_template": early["id"]}],
        }, format="json")

        versions = self.client.get(self.schedules_url()).json()

        self.assertEqual(
            [(v["valid_from"], v["valid_until"], v["state"]) for v in versions],
            [(d(-30), d(-1), "past"), (d(0), d(13), "current"), (d(14), None, "planned")],
        )

    def test_invalid_requests(self):
        workdays = self.create_template("Workdays", "08:00-09:00")
        foreign = DayTemplate.objects.create(club=create_club(), name="Foreign")
        cases = {
            "past start": {"valid_from": d(-1), "days": [{"weekdays": ["MONDAY"], "day_template": workdays["id"]}]},
            "until before from": {"valid_from": d(5), "valid_until": d(4), "days": [{"weekdays": ["MONDAY"], "day_template": None}]},
            "repeated weekday": {"valid_from": d(0), "days": [
                {"weekdays": ["MONDAY"], "day_template": workdays["id"]}, {"weekdays": ["monday"], "day_template": None}]},
            "unknown weekday": {"valid_from": d(0), "days": [{"weekdays": ["FUNDAY"], "day_template": None}]},
            "unknown template": {"valid_from": d(0), "days": [{"weekdays": ["MONDAY"], "day_template": "11111111-1111-1111-1111-111111111111"}]},
            "other club's template": {"valid_from": d(0), "days": [{"weekdays": ["MONDAY"], "day_template": str(foreign.pk)}]},
            "no days": {"valid_from": d(0), "days": []},
            "empty weekdays": {"valid_from": d(0), "days": [{"weekdays": [], "day_template": None}]},
        }
        for name, body in cases.items():
            with self.subTest(name):
                response = self.client.post(self.schedules_url(), body, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertFalse(CourtSchedule.objects.exists())

    def test_conflict_then_force(self):
        self.workdays_and_weekend()
        booking = create_reservation(
            court=self.court, primary_player=create_user(email="ana@example.com"),
            start_datetime=datetime(2030, 1, 8, 18, tzinfo=SP), end_datetime=datetime(2030, 1, 8, 19, tzinfo=SP),
        )
        evening = self.create_template("Evening", "18:30-20:00")
        body = {"valid_from": d(1), "days": [{"weekdays": WORKDAYS, "day_template": evening["id"]}]}

        conflict = self.client.post(self.schedules_url(), body, format="json")

        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(conflict.json()["conflicting_reservations"], [{
            "id": str(booking.pk), "court": str(self.court.pk), "court_name": "Court 1",
            "start": "2030-01-08T18:00:00-03:00", "end": "2030-01-08T19:00:00-03:00",
            "primary_player": "ana@example.com", "status": "CONFIRMED",
        }])
        self.assertIn("force", conflict.json()["hint"])
        self.assertEqual(CourtSchedule.objects.count(), 1)

        forced = self.client.post(self.schedules_url(), {**body, "force": True}, format="json")

        self.assertEqual(forced.status_code, status.HTTP_201_CREATED, forced.data)
        self.assertEqual([r["id"] for r in forced.json()["cancelled_reservations"]], [str(booking.pk)])
        booking.refresh_from_db()
        self.assertEqual(booking.status, Reservation.Status.CANCELLED)

    def test_delete_planned_version(self):
        self.workdays_and_weekend()
        early = self.create_template("Early", "06:00-07:00")
        planned = self.client.post(self.schedules_url(), {
            "valid_from": d(7), "days": [{"weekdays": ["MONDAY"], "day_template": early["id"]}],
        }, format="json").json()
        past = create_court_schedule(self.court, {}, valid_from=TODAY - timedelta(days=30), valid_until=TODAY - timedelta(days=1))

        url = lambda version_id: reverse("courts:court-schedule-detail", args=[self.court.pk, version_id])  # noqa: E731
        self.assertEqual(self.client.delete(url(past.pk)).status_code, status.HTTP_409_CONFLICT)
        response = self.client.delete(url(planned["id"]))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.json(), {"cancelled_reservations": []})
        self.assertEqual(
            list(CourtSchedule.objects.order_by("valid_from").values_list("valid_from", "valid_until")),
            [(TODAY - timedelta(days=30), TODAY - timedelta(days=1)), (TODAY, None)],
        )

    def test_delete_with_force_query_param(self):
        self.workdays_and_weekend()
        early = self.create_template("Early", "06:00-07:00")
        planned = self.client.post(self.schedules_url(), {
            "valid_from": d(7), "days": [{"weekdays": ["MONDAY"], "day_template": early["id"]}],
        }, format="json").json()
        create_reservation(court=self.court, start_datetime=datetime(2030, 1, 14, 6, tzinfo=SP),
                           end_datetime=datetime(2030, 1, 14, 7, tzinfo=SP))
        url = reverse("courts:court-schedule-detail", args=[self.court.pk, planned["id"]])

        self.assertEqual(self.client.delete(url).status_code, status.HTTP_409_CONFLICT)
        response = self.client.delete(f"{url}?force=true")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()["cancelled_reservations"]), 1)


class CopyScheduleAPITests(ScheduleAPITestCase):
    def setUp(self):
        super().setUp()
        self.workdays_and_weekend()
        self.others = [create_court(club=self.club, name=f"Court {n}") for n in (2, 3)]
        self.url = reverse("courts:court-schedule-copy", args=[self.court.pk])

    def test_copy_to_other_courts(self):
        response = self.client.post(self.url, {"courts": [str(c.pk) for c in self.others], "valid_from": d(1)}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual({s["court"] for s in response.json()["schedules"]}, {str(c.pk) for c in self.others})
        for court in self.others:
            self.assertEqual(self.available(d(5), court), ["08:00-09:30", "09:30-11:00"])

    def test_courts_outside_your_clubs_are_unknown(self):
        response = self.client.post(self.url, {"courts": [str(create_court().pk)], "valid_from": d(1)}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unknown courts", str(response.data["courts"]))

    def test_site_staff_still_cannot_copy_across_clubs(self):
        self.client.force_authenticate(create_superuser())

        response = self.client.post(self.url, {"courts": [str(create_court().pk)], "valid_from": d(1)}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"courts": "All courts must belong to the source court's club."})

class ScheduleOverrideAPITests(ScheduleAPITestCase):
    def setUp(self):
        super().setUp()
        self.workdays, self.weekend, _ = self.workdays_and_weekend()

    def test_put_get_delete(self):
        response = self.client.put(self.override_url(d(2)), {"day_template": self.weekend["id"], "type": "TOURNAMENT", "notes": "Junior club championship"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.json()["date"], d(2))
        self.assertEqual(response.json()["day_template"]["name"], "Weekend")
        self.assertEqual(response.json()["slots"], slot_list("08:00-09:30", "09:30-11:00"))
        self.assertEqual(self.available(d(2)), ["08:00-09:30", "09:30-11:00"])
        override = self.client.get(self.override_url(d(2))).json()
        self.assertEqual((override["type"], override["notes"]), ("TOURNAMENT", "Junior club championship"))

        self.assertEqual(self.client.delete(self.override_url(d(2))).status_code, status.HTTP_200_OK)
        self.assertEqual(self.available(d(2)), ["07:00-08:00", "08:00-09:00", "18:00-19:00", "19:00-20:00"])

    def test_closed_day(self):
        response = self.client.put(self.override_url(d(2)), {"day_template": None, "type": "WEATHER", "notes": "Rain"}, format="json")

        self.assertEqual(response.json()["day_template"], None)
        self.assertEqual(response.json()["slots"], [])
        self.assertEqual(self.available(d(2)), [])

    def test_list_defaults_to_today_onwards(self):
        create_schedule_override(self.court, TODAY - timedelta(days=1), None, notes="Yesterday")
        for offset in (0, 3, 10):
            self.client.put(self.override_url(d(offset)), {"day_template": None, "type": "WEATHER"}, format="json")
        url = reverse("courts:schedule-override-list", args=[self.court.pk])

        self.assertEqual([o["date"] for o in self.client.get(url).json()], [d(0), d(3), d(10)])
        self.assertEqual([o["date"] for o in self.client.get(url, {"date_from": d(-5), "date_to": d(3)}).json()], [d(-1), d(0), d(3)])

    def test_past_dates(self):
        create_schedule_override(self.court, TODAY - timedelta(days=1), None)

        self.assertEqual(self.client.put(self.override_url(d(-1)), {"day_template": None, "type": "WEATHER"}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.delete(self.override_url(d(-1))).status_code, status.HTTP_409_CONFLICT)

    def test_conflict_then_force(self):
        create_reservation(court=self.court, start_datetime=datetime(2030, 1, 9, 7, tzinfo=SP),
                           end_datetime=datetime(2030, 1, 9, 8, tzinfo=SP))

        conflict = self.client.put(self.override_url(d(2)), {"day_template": None, "type": "WEATHER"}, format="json")
        forced = self.client.put(self.override_url(d(2)), {"day_template": None, "type": "WEATHER", "force": True}, format="json")

        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(forced.status_code, status.HTTP_200_OK)
        self.assertEqual(len(forced.json()["cancelled_reservations"]), 1)

    def test_bad_urls(self):
        self.assertEqual(self.client.get(self.override_url("2030-02-30")).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(self.override_url(d(20))).status_code, status.HTTP_404_NOT_FOUND)  # none set

    def test_unknown_template(self):
        response = self.client.put(self.override_url(d(2)), {"day_template": "11111111-1111-1111-1111-111111111111", "type": "OTHER"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ScheduleOverride.objects.exists())


class MidnightBookingAPITests(ScheduleAPITestCase):
    def test_book_the_last_slot_until_midnight(self):
        late = self.create_template("Late", "22:00-23:00", "23:00-24:00")
        self.client.post(self.schedules_url(), {"valid_from": d(0), "days": [{"weekdays": WORKDAYS, "day_template": late["id"]}]}, format="json")

        self.assertEqual(self.available(d(1)), ["22:00-23:00", "23:00-23:59"])
        self.client.force_authenticate(create_user())
        response = self.client.post(reverse("bookings:reservation-list"), {
            "court": str(self.court.pk), "date": d(1), "start_time": "23:00", "end_time": "24:00",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.json()["end"], "2030-01-08T23:59:59.999999-03:00")
        self.assertEqual(response.json()["total_price"], "100.00")  # a full hour


class SchedulePermissionTests(ScheduleAPITestCase):
    def urls(self):
        template = DayTemplate.objects.create(club=self.club, name="T")
        return [
            ("get", self.templates_url()), ("post", self.templates_url()),
            ("get", self.template_url(template)), ("patch", self.template_url(template)),
            ("get", self.schedules_url()), ("post", self.schedules_url()),
            ("post", reverse("courts:court-schedule-copy", args=[self.court.pk])),
            ("get", reverse("courts:schedule-override-list", args=[self.court.pk])),
            ("put", self.override_url(d(2))),
        ]

    def test_non_admins_get_404(self):
        groundskeeper = create_user()
        create_club_staff_role(user=groundskeeper, club=self.club, role=ClubStaffRole.Role.GROUNDSKEEPER)
        other_admin = create_user()
        create_club_staff_role(user=other_admin, club=create_club(), role=ClubStaffRole.Role.ADMIN)
        former_admin = create_user()  # the ADMIN role must be in effect today
        create_club_staff_role(user=former_admin, club=self.club, role=ClubStaffRole.Role.ADMIN,
                               start_date=TODAY - timedelta(days=30), end_date=TODAY - timedelta(days=1))
        future_admin = create_user()
        create_club_staff_role(user=future_admin, club=self.club, role=ClubStaffRole.Role.ADMIN,
                               start_date=TODAY + timedelta(days=1))
        urls = self.urls()
        users = {"player": create_user(), "groundskeeper": groundskeeper, "other club's admin": other_admin,
                 "former admin": former_admin, "future admin": future_admin}
        for name, user in users.items():
            self.client.force_authenticate(user)
            for method, url in urls:
                with self.subTest(name=name, method=method, url=url):
                    self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_gets_403(self):
        self.client.force_authenticate(None)
        for method, url in self.urls():
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, status.HTTP_403_FORBIDDEN)

    def test_site_staff_can_manage_any_club(self):
        self.client.force_authenticate(create_superuser())

        self.assertEqual(self.client.get(self.templates_url()).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(self.schedules_url()).status_code, status.HTTP_200_OK)
