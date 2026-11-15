"""
Tests for the `smoke_api` command, run against a live server thread (real
HTTP), like it runs against a deployed instance.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.test import LiveServerTestCase, override_settings
from django.utils import timezone

from apps.bookings.models import BookingRule, Reservation
from apps.common.management.commands import smoke_api as smoke
from apps.common.tests.factories import create_booking_rule, create_club, create_court, create_reservation, create_user
from apps.courts.models import Club
from apps.users.models import User
from apps.users.services import picture_storage


def login_session(user) -> str:
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session.create()
    return session.session_key


class SmokeAPITests(LiveServerTestCase):
    def run_smoke(self, **options) -> str:
        out = StringIO()
        options.setdefault("force", True)  # the test runner sets DEBUG=False
        call_command("smoke_api", base_url=self.live_server_url, stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    def assert_no_smoke_data(self):
        self.assertFalse(smoke.smoke_users().exists())
        self.assertFalse(smoke.smoke_clubs().exists())

    def test_passes_and_leaves_no_data(self):
        other_club, other_user = create_club(name="Real Club"), create_user()
        other_rule = create_booking_rule(other_club)

        output = self.run_smoke()

        self.assertIn("API smoke test passed.", output)
        for step in ("courts: POST, GET list, GET, PATCH, DELETE", "GET /clubs/search/", "GET /players/search/", "PUT /me/profile/picture/",
                     "DELETE /me/profile/picture/", "GET /booking-rule-kinds/", "booking rules: POST",
                     "GET /courts/ shows is_singles_only", "POST /reservations/ with the guest by player id",
                     "GET /reservations/?player=<id>", "?force=true unlinks", "DELETE /clubs/<club>/booking-rules/",
                     "POST /auth/signup/", "POST /auth/login/ with the password", "POST /auth/login/code/",
                     "POST /auth/password/reset/"):
            self.assertIn(step, output)
        self.assert_no_smoke_data()
        self.assertFalse(Session.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertEqual(list(BookingRule.objects.all()), [other_rule])
        self.assertTrue(Club.objects.filter(pk=other_club.pk).exists())
        self.assertTrue(User.objects.filter(pk=other_user.pk).exists())

    def test_removes_leftovers_of_a_killed_run_first(self):
        users = [create_user(email=email) for email in smoke.SMOKE_EMAILS.values()]
        leftover_session = login_session(users[0])
        club = create_club(name=smoke.SMOKE_CLUB_NAME)
        court = create_court(club=club)
        create_reservation(court=court, primary_player=users[1],
                           start_datetime=timezone.now() + timedelta(days=1),
                           end_datetime=timezone.now() + timedelta(days=1, hours=1))
        picture = picture_storage().save(f"players/{users[1].pk}/picture-leftover.png", ContentFile(b"x"))
        other_session = login_session(create_user())

        output = self.run_smoke()

        self.assertIn(
            f"removed leftovers of a previous run: 1 session, 1 reservation, 1 club, 1 picture, {len(users)} users", output
        )
        self.assertIn("API smoke test passed.", output)
        self.assert_no_smoke_data()
        self.assertFalse(picture_storage().exists(picture))
        self.assertFalse(Session.objects.filter(pk=leftover_session).exists())
        self.assertTrue(Session.objects.filter(pk=other_session).exists())

    def test_cleans_up_when_a_check_fails(self):
        with mock.patch.object(smoke, "MORNING_SLOTS", [{"start": "10:00", "end": "09:00"}]):  # invalid template
            with self.assertRaisesMessage(CommandError, "day template"):
                self.run_smoke()

        self.assert_no_smoke_data()
        self.assertFalse(Session.objects.exists())

    def test_fails_when_server_is_unreachable(self):
        with self.assertRaisesMessage(CommandError, "Is the server running?"):
            call_command("smoke_api", base_url="http://127.0.0.1:9", force=True, timeout=2,
                         stdout=StringIO(), stderr=StringIO())

        self.assert_no_smoke_data()

    @override_settings(DEBUG=False)
    def test_refuses_without_force_when_debug_is_off(self):
        with self.assertRaisesMessage(CommandError, "Use --force"):
            self.run_smoke(force=False)

        self.assertFalse(User.objects.exists())
