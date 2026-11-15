"""
python manage.py smoke_club_registration [--base-url URL] [--timeout S] [--force]

Smoke test of the club registration, review and editing flow against a
*running* instance: real HTTP, session login with CSRF, JSON API — then
verifies what the server stored by reading the same database. Self-registered
clubs await approval (hidden from others), their registrant is their club
admin, and site staff approve / reject them.

The command must use the same database as the server (same DATABASE_URL):
it creates the test user there and cleans up there. Everything it creates is
marked, so cleanup never touches other data:

  * users SMOKE_EMAIL and SMOKE_OTHER_EMAIL (".invalid" is a reserved TLD —
    no real user has it), created with fresh random passwords on every run;
  * clubs named exactly SMOKE_CLUB_NAME;
  * the smoke user's login sessions.

Cleanup runs first (removing leftovers of a run that was killed) and again
at the end, whether the flow passed or failed. Assumes one smoke run at a
time per database.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError

from apps.common.smoke import SmokeClient, SmokeFailure, check, sessions_of
from apps.courts.models import Club, ClubStaffRole
from apps.users.models import User

SMOKE_EMAIL = "smoke-test@courtcontrol.invalid"
SMOKE_OTHER_EMAIL = "smoke-test-other@courtcontrol.invalid"  # not the registrant
SMOKE_CLUB_NAME = "Smoke Test Club (courtcontrol-smoke)"
SMOKE_ADDRESS = "Smoke Street, 1"

CLUBS_PATH = "/api/v1/clubs/"
HEALTH_PATH = "/health/"

# Slug rule: name -> name + address -> name + address + number.
SLUG_BY_NAME = "smoke-test-club-courtcontrol-smoke"
SLUG_BY_NAME_AND_ADDRESS = f"{SLUG_BY_NAME}-smoke-street-1"
SLUG_NUMBERED = f"{SLUG_BY_NAME_AND_ADDRESS}-2"


def smoke_clubs():
    return Club.objects.filter(name=SMOKE_CLUB_NAME)


def smoke_users():
    return User.objects.filter(email__in=[SMOKE_EMAIL, SMOKE_OTHER_EMAIL])


def model_default(field: str):
    return Club._meta.get_field(field).default


class Command(BaseCommand):
    help = (
        "Smoke-test club registration and editing against a running server (HTTP + session "
        "login), cleaning up its own test user and clubs before and after."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            default="http://127.0.0.1:8000",
            help="Root URL of the running server (default: %(default)s).",
        )
        parser.add_argument(
            "--timeout", type=float, default=10.0, help="Per-request timeout in seconds."
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even when DEBUG is False (it writes, then deletes, a user and clubs).",
        )

    def handle(self, *args, base_url: str, timeout: float, force: bool, **options):
        if not settings.DEBUG and not force:
            raise CommandError(
                "Refusing to run with DEBUG=False: the smoke test creates (and then deletes) "
                "a user and clubs in this database. Use --force to run it against "
                "staging/production on purpose."
            )

        self.client = SmokeClient(base_url, timeout=timeout)
        self.verbose = options["verbosity"] >= 1
        if self.verbose:
            self.stdout.write(f"Club registration smoke test against {self.client.base_url}")

        leftovers = self._cleanup()
        if leftovers:
            self.stdout.write(self.style.WARNING(f"  ! removed leftovers of a previous run: {leftovers}"))

        failure: SmokeFailure | None = None
        interrupted = False
        try:
            self._run_flow()
        except SmokeFailure as exc:
            failure = exc
        except KeyboardInterrupt:  # Ctrl-C: still clean up, without a traceback
            interrupted = True
        finally:
            self._cleanup()
            remaining = smoke_clubs().count() + smoke_users().count()
            if remaining:  # never expected; say so loudly rather than leave data silently
                self.stderr.write(self.style.ERROR(f"  ✖ cleanup left {remaining} smoke rows behind"))
            else:
                self._ok("cleanup: smoke users, clubs and sessions removed")

        if interrupted:
            raise CommandError("Smoke test interrupted.", returncode=130)
        if failure is not None:
            raise CommandError(f"Smoke test FAILED: {failure}")
        self.stdout.write(self.style.SUCCESS("Smoke test passed."))

    # --- flow ------------------------------------------------------------------

    def _run_flow(self):
        client = self.client

        response = client.get(HEALTH_PATH)
        check(response.status == 200 and response.json().get("status") == "ok",
              f"health check: {response.describe()}")
        self._ok("server is up (/health/)")

        password = secrets.token_urlsafe(24)
        User.objects.create_user(email=SMOKE_EMAIL, password=password)
        self._ok(f"created smoke user {SMOKE_EMAIL}")

        response = client.post_json(CLUBS_PATH, {"name": SMOKE_CLUB_NAME, "address": SMOKE_ADDRESS})
        check(response.status == 403, f"anonymous registration should be 403, got {response.describe()}")
        check(not smoke_clubs().exists(), "anonymous registration stored a club")
        self._ok("anonymous registration is rejected (403)")

        response = client.login(SMOKE_EMAIL, password)
        if response.status == 200:  # the login form re-rendered: credentials rejected
            raise SmokeFailure(
                "login rejected the smoke user's credentials — does this command use the "
                "same database as the server (DATABASE_URL)?"
            )
        check(response.status == 302 and client.session_key, f"login failed: {response.describe()}")
        self._ok("logged in (session + CSRF)")

        # Only name and address are taken; settings sent along are ignored.
        first = self._register(
            {"name": SMOKE_CLUB_NAME, "address": SMOKE_ADDRESS,
             "access_type": "MEMBERS_ONLY", "is_active": False, "slug": "chosen-by-client"},
            expected_slug=SLUG_BY_NAME,
        )
        self._check_defaults(first)
        check(first["approval_status"] == "PENDING", f"a self-registered club awaits approval: {first}")
        check(ClubStaffRole.objects.current().filter(
            club_id=first["id"], user__email=SMOKE_EMAIL, role=ClubStaffRole.Role.ADMIN).exists(),
            "the registrant isn't the club's admin")
        self._ok(f"registered club -> slug {SLUG_BY_NAME!r}, PENDING, registrant is its club admin, "
                 "defaults applied, extra fields ignored")

        self._register({"name": SMOKE_CLUB_NAME, "address": SMOKE_ADDRESS},
                       expected_slug=SLUG_BY_NAME_AND_ADDRESS)
        self._ok(f"same name -> slug {SLUG_BY_NAME_AND_ADDRESS!r}")

        third = self._register({"name": SMOKE_CLUB_NAME, "address": SMOKE_ADDRESS},
                               expected_slug=SLUG_NUMBERED)
        self._ok(f"same name + address -> slug {SLUG_NUMBERED!r}")

        self._check_edit_and_review(first["id"], third["id"])

        for body, field in [
            ({"name": SMOKE_CLUB_NAME}, "address"),
            ({"name": "   ", "address": SMOKE_ADDRESS}, "name"),
        ]:
            response = client.post_json(CLUBS_PATH, body)
            check(response.status == 400 and field in response.json(),
                  f"invalid body {body} should be 400 with a {field!r} error, got {response.describe()}")
        check(smoke_clubs().count() == 3, f"expected 3 smoke clubs stored, found {smoke_clubs().count()}")
        self._ok("missing address / blank name are rejected (400), nothing stored")

        session_key = client.session_key
        response = client.logout()
        check(response.status in (200, 302), f"logout: {response.describe()}")
        check(not Session.objects.filter(pk=session_key).exists(), "session still exists after logout")
        response = client.post_json(CLUBS_PATH, {"name": SMOKE_CLUB_NAME, "address": SMOKE_ADDRESS})
        check(response.status == 403, f"registration after logout should be 403, got {response.describe()}")
        self._ok("logged out; registration rejected again (403)")

    def _register(self, body: dict, *, expected_slug: str) -> dict:
        response = self.client.post_json(CLUBS_PATH, body)
        check(response.status == 201, f"registration should be 201, got {response.describe()}")
        data = response.json()
        check(data.get("slug") == expected_slug,
              f"expected slug {expected_slug!r}, got {data.get('slug')!r}")
        check((data.get("name"), data.get("address")) == (SMOKE_CLUB_NAME, SMOKE_ADDRESS),
              f"response name/address mismatch: {data}")

        # What the server stored — proves the write went to this database.
        stored = Club.objects.filter(pk=data.get("id")).first()
        check(stored is not None, f"club {data.get('id')} not found in the database")
        check((stored.name, stored.address, stored.slug) == (SMOKE_CLUB_NAME, SMOKE_ADDRESS, expected_slug),
              f"stored club differs from the response: {stored.name!r}, {stored.address!r}, {stored.slug!r}")
        return data

    def _check_edit_and_review(self, club_id: str, other_club_id: str):
        """
        The registrant (its club admin) edits the pending club; another user
        can't see it; site staff reject one club and approve the other; then
        everyone sees the approved one, but only its admins edit it.
        """
        club_path = f"{CLUBS_PATH}{club_id}/"
        other = SmokeClient(self.client.base_url, timeout=self.client.timeout)
        password = secrets.token_urlsafe(24)
        User.objects.create_user(email=SMOKE_OTHER_EMAIL, password=password)
        check(other.login(SMOKE_OTHER_EMAIL, password).status == 302, "second smoke user can't log in")
        self.other = other

        response = other.get(club_path)
        check(response.status == 404, f"another user should not see a pending club, got {response.describe()}")
        self._ok("a pending club is hidden from other users (404)")

        response = self.client.request(
            "PATCH", club_path,
            json_body={"timezone": "Europe/Lisbon", "cancellation_window_hours": 1},
            csrf=True,
        )
        check(response.status == 200, f"club admin edit should be 200, got {response.describe()}")
        data = response.json()
        check(
            (data.get("timezone"), data.get("cancellation_window_hours"), data.get("name"))
            == ("Europe/Lisbon", 1, SMOKE_CLUB_NAME),
            f"edit response mismatch: {data}",
        )
        stored = Club.objects.get(pk=club_id)
        check(
            (stored.timezone, stored.cancellation_window_hours) == ("Europe/Lisbon", 1),
            f"edit wasn't stored: {stored.timezone!r}, {stored.cancellation_window_hours}",
        )
        self._ok("the registrant (club admin) edits the pending club; stored")

        response = self.client.request("PATCH", club_path, json_body={"name": "Hijacked"}, csrf=True)
        check(
            response.status == 400 and "contact support" in str(response.json().get("name", "")),
            f"editing the name should be a 400 pointing at support, got {response.describe()}",
        )
        check(Club.objects.get(pk=club_id).name == SMOKE_CLUB_NAME, "the name was changed")
        self._ok("editing name/address is rejected (400, points at support)")

        # Site staff review. Takes effect on the next request — no extra login
        # (the session only holds the user id).
        User.objects.filter(email=SMOKE_EMAIL).update(is_staff=True)
        response = self.client.get(f"{CLUBS_PATH}pending/")
        pending = [c["id"] for c in response.json().get("results", [])] if response.status == 200 else []
        check({club_id, other_club_id} <= set(pending), f"staff pending queue: {response.describe()}")
        response = self.client.post_json(f"{CLUBS_PATH}{other_club_id}/reject/", {"reason": "Smoke test"})
        check(response.status == 200 and response.json()["approval_status"] == "REJECTED",
              f"reject: {response.describe()}")
        response = self.client.post_json(f"{CLUBS_PATH}{club_id}/approve/", {})
        check(response.status == 200 and Club.objects.get(pk=club_id).approval_status == Club.ApprovalStatus.APPROVED,
              f"approve: {response.describe()}")
        self._ok("staff: GET /clubs/pending/, POST …/reject/ (with a reason), POST …/approve/")
        User.objects.filter(email=SMOKE_EMAIL).update(is_staff=False)

        response = other.get(club_path)
        check(
            response.status == 200 and response.json().get("timezone") == "Europe/Lisbon",
            f"GET of the approved club: {response.describe()}",
        )
        response = other.request("PATCH", club_path, json_body={"is_active": False}, csrf=True)
        check(response.status == 403, f"edit by a non-admin should be 403, got {response.describe()}")
        self._ok("approved: any user sees it (with the edit); only its admins edit it (403 for others)")

    def _check_defaults(self, data: dict):
        expected = {
            "timezone": model_default("timezone"),
            "access_type": model_default("access_type"),
            "cancellation_window_hours": model_default("cancellation_window_hours"),
            "is_active": model_default("is_active"),
        }
        actual = {key: data.get(key) for key in expected}
        check(actual == expected, f"new club should have the defaults {expected}, got {actual}")

    # --- cleanup -----------------------------------------------------------------

    def _cleanup(self) -> str:
        """Delete everything this smoke test owns. Returns a summary of what was removed."""
        users = list(smoke_users())
        session_keys = {key for user in users for key in sessions_of(user)}
        for client in (self.client, getattr(self, "other", None)):
            if client is not None and client.session_key:
                session_keys.add(client.session_key)

        counts = {
            "session": Session.objects.filter(pk__in=session_keys).delete()[0],
            "club": smoke_clubs().delete()[1].get("courts.Club", 0),
            "user": User.objects.filter(pk__in=[u.pk for u in users]).delete()[1].get("users.User", 0),
        }
        return ", ".join(f"{n} {what}{'s' if n != 1 else ''}" for what, n in counts.items() if n)

    def _ok(self, message: str):
        if self.verbose:
            self.stdout.write(f"  {self.style.SUCCESS('✔')} {message}")
