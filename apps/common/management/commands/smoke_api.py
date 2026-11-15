"""
python manage.py smoke_api [--base-url URL] [--timeout S] [--force]

Happy-path smoke test of the player, search and booking-rule endpoints
against a *running* instance: real HTTP, session login with CSRF, JSON and a
multipart upload — checking what the server stored by reading the same
database and file storage.

  GET  /api/v1/clubs/search/                 find the smoke club by its address
  GET  /api/v1/players/search/               find the smoke guest (no emails)
  PUT / DELETE /api/v1/me/profile/picture/   upload a picture (metadata stripped), remove it
  GET  /api/v1/booking-rule-kinds/
  POST / GET / PATCH / DELETE /api/v1/clubs/<club>/booking-rules/[<id>/]
  GET  /api/v1/courts/                       the singles flag and the rules on slots
  POST /api/v1/reservations/                 guest given by player id (no emails back)
  GET  /api/v1/reservations/?player=<id>     the guest sees it
  DELETE /api/v1/clubs/<club>/day-templates/<id>/?force=true   linked to a rule
  GET  /api/v1/auth/csrf/, POST /api/v1/auth/signup/[verify/], GET /api/v1/me/,
  POST /api/v1/auth/login/, /auth/login/code/[verify/], /auth/password/reset/[confirm/],
  POST /api/v1/auth/logout/   — codes: see _known_code()

The command must use the same database (and file storage) as the server: it
creates its users, club and courts there and cleans up there. Everything it
creates is marked, so cleanup never touches other data:

  * users  smoke-api-*@courtcontrol.invalid (".invalid" is a reserved TLD),
    with fresh random passwords, and their sessions, picture files and email
    challenges (codes) — including the account the signup creates;
  * the club named exactly SMOKE_CLUB_NAME — with its courts, schedules,
    booking rules and the reservations on its courts.

Cleanup runs first (leftovers of a killed run) and again at the end, whether
the flow passed or failed. One smoke run at a time per database.
"""

from __future__ import annotations

import io
import os
import secrets
from decimal import Decimal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from PIL import Image

from apps.bookings.models import BookingRule, Reservation
from apps.common.smoke import SmokeClient, SmokeFailure, check, sessions_of
from apps.courts.models import Club, Court, DayTemplate
from apps.users.auth import _code_hash
from apps.users.models import EmailChallenge, PlayerProfile, User
from apps.users.services import picture_storage

SMOKE_EMAILS = {
    "staff": "smoke-api-staff@courtcontrol.invalid",  # site staff: approves the smoke club
    "admin": "smoke-api-admin@courtcontrol.invalid",
    "booker": "smoke-api-booker@courtcontrol.invalid",
    "guest": "smoke-api-guest@courtcontrol.invalid",
}
SIGNUP_EMAIL = "smoke-api-signup@courtcontrol.invalid"  # created by the signup flow
KNOWN_CODE = "246810"
# A made-up word, so the searches only find smoke data.
WORD = "zqsmoke"
SMOKE_CLUB_NAME = "Smoke API Club (courtcontrol-smoke)"
SMOKE_ADDRESS = f"{WORD.title()} Avenue, 1, Smoketown"
WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
MORNING_SLOTS = [{"start": f"{h:02}:00", "end": f"{h + 1:02}:00"} for h in range(8, 12)]


def smoke_users():
    return User.objects.filter(email__in=[*SMOKE_EMAILS.values(), SIGNUP_EMAIL])


def smoke_clubs():
    return Club.objects.filter(name=SMOKE_CLUB_NAME)


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 60)).save(buffer, "PNG")
    return buffer.getvalue()


class Command(BaseCommand):
    help = (
        "Smoke-test searches, profile pictures, booking rules and bookings with guests by id against a "
        "running server (HTTP + session login), cleaning up its own users and club before and after."
    )

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                            help="Root URL of the running server (default: %(default)s).")
        parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds.")
        parser.add_argument("--force", action="store_true",
                            help="Run even when DEBUG is False (it writes, then deletes, users and a club).")

    def handle(self, *args, base_url: str, timeout: float, force: bool, **options):
        if not settings.DEBUG and not force:
            raise CommandError(
                "Refusing to run with DEBUG=False: the smoke test creates (and then deletes) users and a "
                "club in this database. Use --force to run it against staging/production on purpose."
            )
        self.base_url, self.timeout = base_url, timeout
        self.clients: dict[str, SmokeClient] = {}
        self.verbose = options["verbosity"] >= 1
        if self.verbose:
            self.stdout.write(f"API smoke test against {base_url.rstrip('/')}")

        leftovers = self._cleanup()
        if leftovers:
            self.stdout.write(self.style.WARNING(f"  ! removed leftovers of a previous run: {leftovers}"))

        failure, interrupted = None, False
        try:
            self._run_flow()
        except SmokeFailure as exc:
            failure = exc
        except KeyboardInterrupt:  # Ctrl-C: still clean up, without a traceback
            interrupted = True
        finally:
            self._cleanup()
            remaining = smoke_users().count() + smoke_clubs().count()
            if remaining:
                self.stderr.write(self.style.ERROR(f"  ✖ cleanup left {remaining} smoke rows behind"))
            else:
                self._ok("cleanup: smoke users, club, rules, bookings, pictures and sessions removed")

        if interrupted:
            raise CommandError("Smoke test interrupted.", returncode=130)
        if failure is not None:
            raise CommandError(f"Smoke test FAILED: {failure}")
        self.stdout.write(self.style.SUCCESS("API smoke test passed."))

    # --- flow ------------------------------------------------------------------

    def _run_flow(self):
        response = SmokeClient(self.base_url, timeout=self.timeout).get("/health/")
        check(response.status == 200, f"health check: {response.describe()}")
        self._ok("server is up (/health/)")

        self._create_users()
        admin, booker, guest = (self.clients[role] for role in ("admin", "booker", "guest"))
        club, courts, template = self._set_up_club(admin)
        self._searches(booker, club)
        self._picture(booker)
        rule = self._booking_rules(admin, club, template)
        self._listing(booker, club, courts, rule)
        self._booking(booker, guest, courts["regular"])
        self._forced_template_delete(admin, club)
        self._accounts()

        response = admin.delete(f"/api/v1/clubs/{club['id']}/booking-rules/{rule['id']}/")
        check(response.status == 204 and not BookingRule.objects.filter(pk=rule["id"]).exists(),
              f"DELETE booking rule: {response.describe()}")
        self._ok("DELETE /clubs/<club>/booking-rules/<id>/ -> 204")

        for role, client in self.clients.items():
            response = client.logout()
            check(response.status in (200, 302), f"logout ({role}): {response.describe()}")
        self._ok("logged out")

    def _create_users(self):
        names = {"staff": "Staff", "admin": "Admin", "booker": "Booker", "guest": "Guest"}
        for role, email in SMOKE_EMAILS.items():
            password = secrets.token_urlsafe(24)
            user = User.objects.create_user(email=email, password=password, first_name=WORD.title(),
                                             last_name=names[role], is_staff=role == "staff")
            PlayerProfile.objects.create(user=user, location=f"{WORD.title()}ville")
            client = SmokeClient(self.base_url, timeout=self.timeout)
            response = client.login(email, password)
            if response.status == 200:  # the login form re-rendered: credentials rejected
                raise SmokeFailure(
                    "login rejected a smoke user's credentials — does this command use the same database "
                    "as the server (DATABASE_URL)?"
                )
            check(response.status == 302 and client.session_key, f"login ({role}): {response.describe()}")
            self.clients[role] = client
        self._ok("created and logged in the smoke staff member, club admin, booker and guest")

    def _set_up_club(self, admin: SmokeClient):
        response = admin.post_json("/api/v1/clubs/", {"name": SMOKE_CLUB_NAME, "address": SMOKE_ADDRESS})
        check(response.status == 201 and response.json()["approval_status"] == "PENDING",
              f"club registration: {response.describe()}")
        club = response.json()  # its registrant, the smoke admin, is its club admin
        response = self.clients["staff"].post_json(f"/api/v1/clubs/{club['id']}/approve/", {})
        check(response.status == 200, f"approve the smoke club: {response.describe()}")
        courts = self._courts(admin, club)

        response = admin.post_json(f"/api/v1/clubs/{club['id']}/day-templates/",
                                   {"name": "Smoke mornings", "slots": MORNING_SLOTS})
        check(response.status == 201, f"day template: {response.describe()}")
        template = response.json()
        self.today = datetime.now(ZoneInfo(club["timezone"])).date()
        for court in courts.values():
            response = admin.post_json(f"/api/v1/courts/{court.pk}/schedules/", {
                "valid_from": self.today.isoformat(),
                "days": [{"weekdays": WEEKDAYS, "day_template": template["id"]}],
            })
            check(response.status == 201, f"schedule for {court.name}: {response.describe()}")
        self._ok("set up the smoke club: registered (pending) and approved by staff; a day template, weekly schedules")
        return club, courts, template

    def _courts(self, admin: SmokeClient, club: dict) -> dict[str, Court]:
        """The club admin adds, lists, views, edits and deletes courts through the API."""
        path = f"/api/v1/clubs/{club['id']}/courts/"
        ids = {}
        for key, body in (("regular", {"name": "Smoke Court", "hourly_rate": "80.00"}),
                          ("singles", {"name": "Smoke Singles Court", "is_singles_only": True}),
                          ("spare", {"name": "Smoke Spare Court"})):
            response = admin.post_json(path, body)
            check(response.status == 201, f"add court {body['name']!r}: {response.describe()}")
            ids[key] = response.json()["id"]
        response = admin.get(path)
        check(response.status == 200 and {c["id"] for c in response.json()} == set(ids.values()),
              f"list courts: {response.describe()}")
        response = admin.get(f"{path}{ids['singles']}/")
        check(response.status == 200 and response.json()["is_singles_only"], f"view court: {response.describe()}")
        response = admin.patch_json(f"{path}{ids['regular']}/", {"hourly_rate": "90.00", "lighting_fee": "10.00"})
        check(response.status == 200 and Court.objects.get(pk=ids["regular"]).hourly_rate == Decimal("90.00"),
              f"edit court: {response.describe()}")
        response = admin.delete(f"{path}{ids['spare']}/")
        check(response.status == 204 and not Court.objects.filter(pk=ids["spare"]).exists(),
              f"delete court: {response.describe()}")
        self._ok("courts: POST, GET list, GET, PATCH, DELETE /clubs/<club>/courts/ (club admin)")
        return {key: Court.objects.get(pk=ids[key]) for key in ("regular", "singles")}

    def _searches(self, booker: SmokeClient, club: dict):
        response = booker.get("/api/v1/clubs/search/", {"q": f"{WORD} avenue"})
        results = response.json().get("results", []) if response.status == 200 else []
        check([c["id"] for c in results] == [club["id"]], f"club search by address: {response.describe()}")
        self._ok("GET /clubs/search/ finds the club by its address")

        response = booker.get("/api/v1/players/search/", {"q": f"{WORD} guest"})
        check(response.status == 200, f"player search: {response.describe()}")
        results = response.json()["results"]
        guest_id = str(smoke_users().get(email=SMOKE_EMAILS["guest"]).pk)
        check([p["id"] for p in results] == [guest_id], f"player search should find the guest: {results}")
        check(b"@" not in response.body, "player search returned an email")
        self.guest_id = guest_id
        self._ok("GET /players/search/ finds the guest by name, no emails")

    def _picture(self, booker: SmokeClient):
        path = "/api/v1/me/profile/picture/"
        response = booker.request("PUT", path, files={"picture": ("me.png", png(), "image/png")}, csrf=True)
        check(response.status == 200, f"picture upload: {response.describe()}")
        key = response.json()["picture_key"]
        profile = PlayerProfile.objects.get(user__email=SMOKE_EMAILS["booker"])
        check(profile.picture_key == key and picture_storage().exists(key),
              f"picture {key!r} not stored (profile key {profile.picture_key!r})")
        self._ok("PUT /me/profile/picture/ stores the picture")

        response = booker.delete(path)
        check(response.status == 204, f"picture delete: {response.describe()}")
        profile.refresh_from_db()
        check(profile.picture_key == "" and not picture_storage().exists(key), "picture not removed")
        self._ok("DELETE /me/profile/picture/ removes it")

    def _booking_rules(self, admin: SmokeClient, club: dict, template: dict):
        response = admin.get("/api/v1/booking-rule-kinds/")
        kinds = {k["kind"] for k in response.json()} if response.status == 200 else set()
        check("max_active_bookings" in kinds, f"rule kinds: {response.describe()}")
        self._ok(f"GET /booking-rule-kinds/ lists {len(kinds)} kinds")

        rules_path = f"/api/v1/clubs/{club['id']}/booking-rules/"
        slot_10 = next(s["id"] for s in template["slots"] if s["start"] == "10:00")
        response = admin.post_json(rules_path, {
            "name": "Smoke limit", "kind": "max_active_bookings", "params": {"max": 2}, "slots": [slot_10],
        })
        check(response.status == 201, f"create booking rule: {response.describe()}")
        rule = response.json()
        check(BookingRule.objects.filter(pk=rule["id"], club_id=club["id"]).exists(), "rule not stored")

        response = admin.get(rules_path)
        check(response.status == 200 and [r["id"] for r in response.json()] == [rule["id"]],
              f"list booking rules: {response.describe()}")
        response = admin.get(f"{rules_path}{rule['id']}/")
        check(response.status == 200 and response.json()["summary"] == "Max 2 upcoming bookings",
              f"booking rule detail: {response.describe()}")
        response = admin.patch_json(f"{rules_path}{rule['id']}/", {"name": "Smoke limit (renamed)"})
        check(response.status == 200 and BookingRule.objects.get(pk=rule["id"]).name == "Smoke limit (renamed)",
              f"rename booking rule: {response.describe()}")
        self._ok("booking rules: POST, GET list, GET detail, PATCH")
        return response.json()

    def _listing(self, booker: SmokeClient, club: dict, courts: dict, rule: dict):
        self.match_day = self.today + timedelta(days=1)
        response = booker.get("/api/v1/courts/", {"club": club["id"], "date": self.match_day.isoformat()})
        check(response.status == 200, f"courts listing: {response.describe()}")
        listed = {c["name"]: c for c in response.json()["results"]}
        check({name: c["is_singles_only"] for name, c in listed.items()}
              == {"Smoke Court": False, "Smoke Singles Court": True}, f"singles flags: {listed}")
        slots = {s["start"][11:16]: s["rules"] for s in listed["Smoke Court"]["available_slots"]}
        check(slots.get("10:00") == [rule["id"]] and slots.get("09:00") == [],
              f"rule ids on slots: {slots}")
        check([r["id"] for r in listed["Smoke Court"]["booking_rules"]] == [rule["id"]],
              f"court booking_rules: {listed['Smoke Court']['booking_rules']}")
        self._ok("GET /courts/ shows is_singles_only and the rule on its slot")

    def _booking(self, booker: SmokeClient, guest: SmokeClient, court: Court):
        response = booker.post_json("/api/v1/reservations/", {
            "court": str(court.pk), "date": self.match_day.isoformat(),
            "start_time": "10:00", "end_time": "11:00", "guests": [self.guest_id],
        })
        check(response.status == 201, f"booking with a guest by id: {response.describe()}")
        booking = response.json()
        check([g["id"] for g in booking["guests"]] == [self.guest_id], f"guests: {booking['guests']}")
        check(b"@" not in response.body, "the booking response contains an email")
        check(list(Reservation.objects.get(pk=booking["id"]).guests.values_list("email", flat=True))
              == [SMOKE_EMAILS["guest"]], "guest not stored on the booking")
        self._ok("POST /reservations/ with the guest by player id; no emails in the response")

        booker_id = str(smoke_users().get(email=SMOKE_EMAILS["booker"]).pk)
        response = guest.get("/api/v1/reservations/", {"player": booker_id})
        results = response.json().get("results", []) if response.status == 200 else []
        check([(r["id"], r["role"]) for r in results] == [(booking["id"], "guest")],
              f"guest's reservations filtered by the booker's id: {response.describe()}")
        self._ok("GET /reservations/?player=<id>: the guest sees it")

    def _forced_template_delete(self, admin: SmokeClient, club: dict):
        response = admin.post_json(f"/api/v1/clubs/{club['id']}/day-templates/",
                                   {"name": "Smoke spare", "slots": MORNING_SLOTS[:1]})
        check(response.status == 201, f"spare day template: {response.describe()}")
        spare = response.json()
        response = admin.post_json(f"/api/v1/clubs/{club['id']}/booking-rules/", {
            "name": "Smoke spare only", "kind": "booking_types", "params": {"allowed": ["LESSON"]},
            "day_templates": [spare["id"]],
        })
        check(response.status == 201, f"rule linked to the spare template: {response.describe()}")
        rule_id = response.json()["id"]

        response = admin.delete(f"/api/v1/clubs/{club['id']}/day-templates/{spare['id']}/", {"force": "true"})
        check(response.status == 200, f"forced template delete: {response.describe()}")
        report = response.json()
        check([r["id"] for r in report["deactivated_booking_rules"]] == [rule_id], f"report: {report}")
        check(not DayTemplate.objects.filter(pk=spare["id"]).exists()
              and not BookingRule.objects.get(pk=rule_id).is_active,
              "template not deleted, or its rule not deactivated")
        self._ok("DELETE /day-templates/<id>/?force=true unlinks and deactivates the rule")

    def _accounts(self):
        """Sign up, log in (password, code), reset the password — a fresh client, like an app."""
        client = SmokeClient(self.base_url, timeout=self.timeout)
        response = client.get("/api/v1/auth/csrf/")
        check(response.status == 200 and client.cookie("csrftoken"), f"CSRF token: {response.describe()}")

        password, new_password = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        challenge = self._challenge(client, "/api/v1/auth/signup/", {
            "email": SIGNUP_EMAIL, "first_name": WORD.title(), "last_name": "Signup", "password": password})
        response = client.post_json("/api/v1/auth/signup/verify/", {"challenge_id": challenge, "code": KNOWN_CODE})
        check(response.status == 201 and response.json()["email"] == SIGNUP_EMAIL,
              f"verify sign-up: {response.describe()}")
        check(User.objects.filter(email=SIGNUP_EMAIL, player_profile__isnull=False).exists(), "account not created")
        response = client.get("/api/v1/me/")
        me = response.json() if response.status == 200 else {}
        check(
            response.status == 200
            and me.get("email") == SIGNUP_EMAIL
            and me.get("is_player") is True
            and isinstance(me.get("clubs"), list),
            f"GET /me/: {response.describe()}",
        )
        self._logout(client)
        self._ok("POST /auth/signup/ + /auth/signup/verify/ (logged in), GET /me/, POST /auth/logout/")

        response = client.post_json("/api/v1/auth/login/", {"email": SIGNUP_EMAIL, "password": password})
        check(response.status == 200, f"password login: {response.describe()}")
        self._logout(client)
        self._ok("POST /auth/login/ with the password")

        challenge = self._challenge(client, "/api/v1/auth/login/code/", {"email": SIGNUP_EMAIL})
        response = client.post_json("/api/v1/auth/login/code/verify/", {"challenge_id": challenge, "code": KNOWN_CODE})
        check(response.status == 200, f"code login: {response.describe()}")
        self._logout(client)
        self._ok("POST /auth/login/code/ + /auth/login/code/verify/")

        challenge = self._challenge(client, "/api/v1/auth/password/reset/", {"email": SIGNUP_EMAIL})
        response = client.post_json("/api/v1/auth/password/reset/confirm/",
                                    {"challenge_id": challenge, "code": KNOWN_CODE, "new_password": new_password})
        check(response.status == 200, f"password reset: {response.describe()}")
        self._logout(client)
        response = client.post_json("/api/v1/auth/login/", {"email": SIGNUP_EMAIL, "password": new_password})
        check(response.status == 200, f"login with the new password: {response.describe()}")
        self._logout(client)
        self._ok("POST /auth/password/reset/ + /auth/password/reset/confirm/, then the new password works")

    def _challenge(self, client: SmokeClient, path: str, body: dict) -> str:
        response = client.post_json(path, body)
        check(response.status == 202, f"POST {path}: {response.describe()}")
        challenge = response.json()["challenge_id"]
        self._known_code(challenge)
        return challenge

    @staticmethod
    def _known_code(challenge_id: str) -> None:
        """
        The code went by email, which the smoke test can't read: give the
        challenge a known code instead (same database and SECRET_KEY as the
        server), so verifying goes through the real endpoint.
        """
        updated = EmailChallenge.objects.filter(pk=challenge_id).update(code_hash=_code_hash(challenge_id, KNOWN_CODE))
        check(updated == 1, f"challenge {challenge_id} not in this database")

    def _logout(self, client: SmokeClient):
        response = client.request("POST", "/api/v1/auth/logout/", json_body={}, csrf=True)
        check(response.status == 204, f"logout: {response.describe()}")

    # --- cleanup -----------------------------------------------------------------

    def _cleanup(self) -> str:
        """Delete everything this smoke test owns. Returns a summary of what was removed."""
        users = list(smoke_users())
        session_keys = {key for user in users for key in sessions_of(user)}
        session_keys |= {client.session_key for client in self.clients.values() if client.session_key}
        clubs = smoke_clubs()
        counts = {
            "session": Session.objects.filter(pk__in=session_keys).delete()[0],
            # Reservations are PROTECTed history: remove them before their courts.
            "reservation": Reservation.objects.filter(court__club__in=clubs).delete()[1].get("bookings.Reservation", 0),
            "club": clubs.delete()[1].get("courts.Club", 0),
            "code": EmailChallenge.objects.filter(email__in=[*SMOKE_EMAILS.values(), SIGNUP_EMAIL]).delete()[0],
            "picture": sum(self._delete_pictures(user) for user in users),
            "user": User.objects.filter(pk__in=[u.pk for u in users]).delete()[1].get("users.User", 0),
        }
        return ", ".join(f"{n} {what}{'s' if n != 1 else ''}" for what, n in counts.items() if n)

    @staticmethod
    def _delete_pictures(user: User) -> int:
        """The user's stored pictures (players/<user id>/...), including ones a killed run left behind."""
        storage, folder = picture_storage(), f"players/{user.pk}"
        try:
            _dirs, files = storage.listdir(folder)
        except (FileNotFoundError, NotImplementedError):
            return 0
        for name in files:
            storage.delete(f"{folder}/{name}")
        try:  # local disk keeps the emptied folder; storages without folders (S3) have no path()
            os.rmdir(storage.path(folder))
        except (NotImplementedError, OSError):
            pass
        return len(files)

    def _ok(self, message: str):
        if self.verbose:
            self.stdout.write(f"  {self.style.SUCCESS('✔')} {message}")
