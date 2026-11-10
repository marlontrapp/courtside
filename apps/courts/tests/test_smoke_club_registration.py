"""
Tests for the `smoke_club_registration` command, run against a live server
thread (real HTTP), like it runs against a deployed instance.
"""

from io import StringIO
from unittest import mock

from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import LiveServerTestCase, override_settings

from apps.common.tests.factories import create_club, create_user
from apps.courts.management.commands import smoke_club_registration as smoke
from apps.courts.models import Club
from apps.users.models import User


def login_session(user) -> str:
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session.create()
    return session.session_key


class SmokeClubRegistrationTests(LiveServerTestCase):
    def run_smoke(self, **options) -> str:
        out = StringIO()
        options.setdefault("force", True)  # the test runner sets DEBUG=False
        call_command("smoke_club_registration", base_url=self.live_server_url,
                     stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    def assert_no_smoke_data(self):
        self.assertFalse(Club.objects.filter(name=smoke.SMOKE_CLUB_NAME).exists())
        self.assertFalse(User.objects.filter(email=smoke.SMOKE_EMAIL).exists())

    def test_passes_and_leaves_no_data(self):
        other_club = create_club(name="Real Club")

        output = self.run_smoke()

        self.assertIn("Smoke test passed.", output)
        self.assert_no_smoke_data()
        self.assertFalse(Session.objects.exists())
        self.assertTrue(Club.objects.filter(pk=other_club.pk).exists())

    def test_removes_leftovers_of_a_killed_run_first(self):
        leftover_user = create_user(email=smoke.SMOKE_EMAIL)
        leftover_session = login_session(leftover_user)
        for slug in (smoke.SLUG_BY_NAME, smoke.SLUG_BY_NAME_AND_ADDRESS):  # would shift the slugs
            create_club(name=smoke.SMOKE_CLUB_NAME, slug=slug)
        other_session = login_session(create_user())

        output = self.run_smoke()

        self.assertIn("removed leftovers of a previous run: 1 session, 2 clubs, 1 user", output)
        self.assertIn("Smoke test passed.", output)
        self.assert_no_smoke_data()
        self.assertFalse(Session.objects.filter(pk=leftover_session).exists())
        self.assertTrue(Session.objects.filter(pk=other_session).exists())

    def test_cleans_up_when_a_check_fails(self):
        with mock.patch.object(smoke, "SLUG_NUMBERED", "deliberately-wrong"):
            with self.assertRaisesMessage(CommandError, "expected slug 'deliberately-wrong'"):
                self.run_smoke()

        self.assert_no_smoke_data()
        self.assertFalse(Session.objects.exists())

    def test_cleans_up_when_interrupted(self):
        register = smoke.Command._register
        calls = []

        def register_then_ctrl_c(command, *args, **kwargs):
            club = register(command, *args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return club

        with mock.patch.object(smoke.Command, "_register", register_then_ctrl_c):
            with self.assertRaisesMessage(CommandError, "interrupted") as ctx:
                self.run_smoke()

        self.assertEqual(ctx.exception.returncode, 130)
        self.assert_no_smoke_data()
        self.assertFalse(Session.objects.exists())

    def test_fails_when_server_is_unreachable(self):
        with self.assertRaisesMessage(CommandError, "Is the server running?"):
            call_command("smoke_club_registration", base_url="http://127.0.0.1:9", force=True,
                         timeout=2, stdout=StringIO(), stderr=StringIO())

        self.assert_no_smoke_data()

    @override_settings(DEBUG=False)
    def test_refuses_without_force_when_debug_is_off(self):
        with self.assertRaisesMessage(CommandError, "Use --force"):
            self.run_smoke(force=False)

        self.assertFalse(User.objects.exists())
