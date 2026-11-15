"""
Project rule: no API response shows a player's email — except the user's own,
in their own account (GET /api/v1/me/ and the login / sign-up responses).
Players are shown as {id, first_name, last_name}; input accepts a player id or
an email.

This walks every /api/v1/ endpoint — as booker, guest, stranger and club
admin, including 409 conflicts and forced cancellations, where bookings are
summarized — and checks no response contains an email. A second test fails
when an endpoint is added without being walked here (or exempted with a reason).
"""

import io
import re
from contextlib import nullcontext
from datetime import datetime, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from django.core import mail
from django.urls import get_resolver, resolve, reverse
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.tests.factories import (
    add_full_week_slots,
    create_club,
    create_club_staff_role,
    create_court,
    create_player_profile,
    create_reservation,
    create_superuser,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import ClubStaffRole
from apps.users.models import User

SP = ZoneInfo("America/Sao_Paulo")
TODAY = datetime(2030, 1, 7, tzinfo=SP).date()  # Monday
EMAIL_LIKE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Endpoints not walked below, with the reason. Keep it empty if possible.
EXEMPT: dict[str, str] = {}


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def png() -> io.BytesIO:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, "PNG")
    buffer.seek(0)
    buffer.name = "me.png"
    return buffer


def api_view_names() -> set[str]:
    def walk(patterns, prefix="", namespace=None):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                yield from walk(pattern.url_patterns, prefix + str(pattern.pattern), pattern.namespace or namespace)
            elif (prefix + str(pattern.pattern)).startswith("api/v1/"):
                yield f"{namespace}:{pattern.name}" if namespace else pattern.name

    return set(walk(get_resolver().url_patterns))


class NoEmailInResponsesTests(FrozenTimeMixin, APITestCase):
    frozen_now = datetime(2030, 1, 7, 12, 0, tzinfo=SP)

    def setUp(self):
        super().setUp()
        self.responses: list[tuple[str, object, str | None]] = []  # (label, response, own email allowed)
        self.walked: set[str] = set()

        self.club = create_club(name="Leak Test Club", address="Rua A, 1, São Paulo")
        self.court = create_court(club=self.club, name="Court 1")
        self.court2 = create_court(club=self.club, name="Court 2")
        add_full_week_slots(self.court2, open_time=time(8), close_time=time(22))
        self.admin = create_user(email="club.admin@leak.example", first_name="Club", last_name="Admin")
        create_club_staff_role(user=self.admin, club=self.club, role=ClubStaffRole.Role.ADMIN, start_date=TODAY)
        self.staff = create_superuser(email="site.staff@leak.example")
        self.booker = create_user(email="booker@leak.example", first_name="Bruna", last_name="Booker")
        self.guest = create_user(email="guest@leak.example", first_name="Gil", last_name="Guest")
        self.stranger = create_user(email="stranger@leak.example", first_name="Sara", last_name="Stranger")
        for user, location in ((self.booker, "Pinheiros, São Paulo"), (self.guest, "Moema, São Paulo")):
            create_player_profile(user=user, location=location)

    # --- plumbing ----------------------------------------------------------------------

    def call(self, user, method: str, url: str, data=None, expected: int = status.HTTP_200_OK, fmt="json",
             own_email: str | None = None, session=False, **params):
        """
        `user`: forced authentication; `session=True`: use the client's real
        session instead (the account endpoints). `own_email`: the response is
        the requester's own account — that email (only) is allowed in it.
        """
        if not session:
            self.client.force_authenticate(user)
        kwargs = {"format": fmt} if data is not None else {}
        with self.captureOnCommitCallbacks(execute=True) if session else nullcontext():  # code emails
            if method == "get":
                response = self.client.get(url, params)
            else:
                response = getattr(self.client, method)(url, data, **kwargs)
        self.assertEqual(response.status_code, expected, (method, url, getattr(response, "data", None)))
        self.walked.add(resolve(urlparse(url).path).view_name)
        self.responses.append((f"{method.upper()} {url}", response, own_email))
        return response.json() if response.content else None

    def code(self, email: str) -> str:
        return re.search(r"\b(\d{6})\b", [m for m in mail.outbox if m.to == [email]][-1].body).group(1)

    def walk_account_endpoints(self):
        """Sign up, log in (password, code), reset the password, log out — a real session, like a client."""
        self.client.force_authenticate(None)
        account = lambda name: reverse(f"users:{name}")  # noqa: E731
        new = "walk-signup@leak.example"
        self.call(None, "get", account("auth-csrf"), session=True)
        challenge = self.call(None, "post", account("auth-signup"), {"email": new, "first_name": "Walk", "last_name": "Er"},
                              status.HTTP_202_ACCEPTED, session=True)["challenge_id"]
        self.call(None, "post", account("auth-signup-verify"), {"challenge_id": challenge, "code": self.code(new)},
                  status.HTTP_201_CREATED, own_email=new, session=True)
        self.call(None, "get", account("me"), own_email=new, session=True)
        self.call(None, "post", account("auth-logout"), {}, status.HTTP_204_NO_CONTENT, session=True)

        email = self.booker.email
        self.call(None, "post", account("auth-login"), {"email": email, "password": "test-pass-123!"},
                  own_email=email, session=True)
        challenge = self.call(None, "post", account("auth-login-code"), {"email": email}, status.HTTP_202_ACCEPTED,
                              session=True)["challenge_id"]
        self.call(None, "post", account("auth-login-code-verify"), {"challenge_id": challenge, "code": self.code(email)},
                  own_email=email, session=True)
        challenge = self.call(None, "post", account("auth-password-reset"), {"email": email}, status.HTTP_202_ACCEPTED,
                              session=True)["challenge_id"]
        self.call(None, "post", account("auth-password-reset-confirm"),
                  {"challenge_id": challenge, "code": self.code(email), "new_password": "a-New-pass-2030"},
                  own_email=email, session=True)
        self.call(None, "post", account("auth-logout"), {}, status.HTTP_204_NO_CONTENT, session=True)

    def walk_all_endpoints(self):
        admin, booker, guest, stranger, staff = self.admin, self.booker, self.guest, self.stranger, self.staff
        courts = "courts:"

        # Clubs
        registered = self.call(stranger, "post", reverse(f"{courts}club-register"),
                               {"name": "New Club", "address": "Rua B, 2"}, status.HTTP_201_CREATED)
        self.call(stranger, "get", reverse(f"{courts}club-search"), q="leak test")
        self.call(stranger, "get", reverse(f"{courts}club-detail", args=[self.club.pk]))
        self.call(staff, "patch", reverse(f"{courts}club-detail", args=[registered["id"]]), {"cancellation_window_hours": 12})
        # Site staff review the self-registered club (pending)
        self.call(staff, "get", reverse(f"{courts}club-pending"))
        self.call(staff, "post", reverse(f"{courts}club-reject", args=[registered["id"]]), {"reason": "Duplicate"})
        self.call(staff, "post", reverse(f"{courts}club-approve", args=[registered["id"]]), {})

        # Club admin sets up the schedule (templates, weekly version, override list)
        templates = reverse(f"{courts}day-template-list", args=[self.club.pk])
        evening = self.call(admin, "post", templates, {"name": "Evening", "slots": [
            {"start": "18:00", "end": "19:00"}, {"start": "19:00", "end": "20:00"}]}, status.HTTP_201_CREATED)
        late = self.call(admin, "post", templates, {"name": "Late", "slots": [{"start": "20:00", "end": "21:00"}]},
                         status.HTTP_201_CREATED)
        self.call(admin, "get", templates)
        self.call(admin, "get", reverse(f"{courts}day-template-detail", args=[self.club.pk, late["id"]]))
        schedules = reverse(f"{courts}court-schedule-list", args=[self.court.pk])
        version = self.call(admin, "post", schedules, {"valid_from": day(0), "days": [
            {"weekdays": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"], "day_template": evening["id"]}]},
            status.HTTP_201_CREATED)
        self.call(admin, "get", schedules)
        self.call(admin, "get", reverse(f"{courts}court-schedule-detail", args=[self.court.pk, version["id"]]))
        self.call(admin, "get", reverse(f"{courts}schedule-override-list", args=[self.court.pk]))
        club_courts = reverse(f"{courts}club-court-list", args=[self.club.pk])
        spare = self.call(admin, "post", club_courts, {"name": "Spare"}, status.HTTP_201_CREATED)
        self.call(admin, "get", club_courts)
        spare_url = reverse(f"{courts}club-court-detail", args=[self.club.pk, spare["id"]])
        self.call(admin, "get", spare_url)
        self.call(admin, "patch", spare_url, {"hourly_rate": "50.00"})
        self.call(admin, "delete", spare_url, expected=status.HTTP_204_NO_CONTENT)

        # Players find each other and book
        self.call(stranger, "get", reverse(f"{courts}court-list"), date=day(1))
        found = self.call(booker, "get", reverse("users:player-search"), q="gil são paulo")["results"]
        reservations = reverse("bookings:reservation-list")
        booking = self.call(booker, "post", reservations, {
            "court": str(self.court.pk), "date": day(1), "start_time": "18:00", "end_time": "19:00",
            "booking_type": "DOUBLES", "guests": [found[0]["id"]]}, status.HTTP_201_CREATED)
        by_email = self.call(booker, "post", reservations, {
            "court": str(self.court.pk), "date": day(2), "start_time": "18:00", "end_time": "19:00",
            "guests": ["guest@leak.example"]}, status.HTTP_201_CREATED)
        self.call(booker, "get", reservations)
        self.call(guest, "get", reservations, player=str(booker.pk))
        self.call(booker, "post", reverse("bookings:reservation-cancel", args=[by_email["id"]]), {"reason": "rain"})

        # Schedule changes that hit bookings: 409 summaries, then forced cancellations
        late_version = {"valid_from": day(1), "days": [{"weekdays": ["TUESDAY"], "day_template": late["id"]}]}
        self.call(admin, "post", schedules, late_version, status.HTTP_409_CONFLICT)
        override = reverse(f"{courts}schedule-override-detail", args=[self.court.pk, day(1)])
        self.call(admin, "put", override, {"day_template": None, "type": "WEATHER"}, status.HTTP_409_CONFLICT)
        copy = reverse(f"{courts}court-schedule-copy", args=[self.court.pk])
        other_court_booking = create_reservation(court=self.court2, primary_player=booker,
                                                 start_datetime=datetime(2030, 1, 8, 10, tzinfo=SP),
                                                 end_datetime=datetime(2030, 1, 8, 11, tzinfo=SP))
        other_court_booking.guests.set([guest])
        self.call(admin, "post", copy, {"courts": [str(self.court2.pk)], "valid_from": day(1)}, status.HTTP_409_CONFLICT)
        self.call(admin, "post", copy, {"courts": [str(self.court2.pk)], "valid_from": day(1), "force": True},
                  status.HTTP_201_CREATED)
        court_url = reverse(f"{courts}club-court-detail", args=[self.club.pk, self.court.pk])
        self.call(admin, "patch", court_url, {"is_active": False}, status.HTTP_409_CONFLICT)  # future bookings
        self.call(admin, "put", override, {"day_template": None, "type": "WEATHER", "force": True})
        self.call(admin, "get", override)
        self.assertEqual(booking["guests"][0]["id"], str(guest.pk))

        # Booking rules: the admin manages them, players see them and get rule errors
        rules = reverse("bookings:booking-rule-list", args=[self.club.pk])
        rule = self.call(admin, "post", rules, {"name": "Women's 18:00", "kind": "player_gender",
                                                "params": {"allowed": ["FEMALE"]}, "courts": [str(self.court.pk)],
                                                "slots": [evening["slots"][0]["id"]]},  # Evening 18:00–19:00
                         status.HTTP_201_CREATED)
        rule_url = reverse("bookings:booking-rule-detail", args=[self.club.pk, rule["id"]])
        self.call(admin, "get", rules)
        self.call(admin, "get", rule_url)
        self.call(admin, "patch", rule_url, {"name": "Women's evening"})
        self.call(stranger, "get", reverse("bookings:booking-rule-kinds"))
        self.call(stranger, "get", reverse(f"{courts}court-list"), date=day(3))
        self.call(booker, "post", reservations, {  # booker has no gender set; a named guest: 400, nobody identified
            "court": str(self.court.pk), "date": day(3), "start_time": "18:00", "end_time": "19:00",
            "guests": [str(guest.pk)]}, status.HTTP_400_BAD_REQUEST)
        self.call(admin, "delete", rule_url, expected=status.HTTP_204_NO_CONTENT)

        self.walk_account_endpoints()

        # Own picture
        picture = reverse("users:my-profile-picture")
        self.call(booker, "put", picture, {"picture": png()}, fmt="multipart")
        self.call(booker, "delete", picture, expected=status.HTTP_204_NO_CONTENT)

    # --- tests -----------------------------------------------------------------------

    def test_no_response_contains_an_email(self):
        self.walk_all_endpoints()
        emails = list(User.objects.values_list("email", flat=True))

        self.assertGreater(len(self.responses), 25)
        self.assertTrue(any(own for _, _, own in self.responses), "the own-account responses were walked")
        for label, response, own_email in self.responses:
            body = response.content.decode()
            if own_email:  # the requester's own account: their email (only) is allowed
                self.assertIn(own_email, body, label)
                body = body.replace(own_email, "")
            with self.subTest(label):
                self.assertEqual([e for e in emails if e in body], [])
                self.assertIsNone(EMAIL_LIKE.search(body), body)

    def test_every_api_endpoint_is_checked(self):
        self.walk_all_endpoints()

        missing = api_view_names() - self.walked - set(EXEMPT)
        self.assertEqual(missing, set(), "New endpoint? Walk it in walk_all_endpoints() (or exempt it with a reason).")
