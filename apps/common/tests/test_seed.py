"""Tests for the development seed data and the seed_dev_data command."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from zoneinfo import ZoneInfo

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.bookings.availability import AvailabilityWindow, get_courts_availability
from apps.bookings.models import Reservation
from apps.bookings.services import expire_stale_pending_reservations
from apps.bookings.tasks import send_upcoming_match_reminder_task
from apps.common.seed import CLUBS, PLAYERS, SEED_NOTE, SEED_PASSWORD, seed
from apps.common.tests.utils import FrozenTimeMixin, freeze_time
from apps.courts.models import Club, ClubMembership, Court, ScheduleOverride
from apps.courts.services import get_bookable_courts, get_visible_clubs
from apps.users.models import User

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2030, 1, 7, 10, 0, tzinfo=SAO_PAULO)  # Monday 10:00
TOMORROW = NOW.date() + timedelta(days=1)


def counts() -> dict[str, int]:
    return {
        model.__name__: model.objects.count()
        for model in (User, Club, Court, ClubMembership, Reservation, ScheduleOverride)
    }


class SeedTestCase(FrozenTimeMixin, TestCase):
    frozen_now = NOW

    @classmethod
    def setUpTestData(cls):
        # Seed once per class (each test runs in a rolled-back savepoint);
        # freeze time here too so created_at/relative dates match the tests.
        with freeze_time(NOW):
            cls.result = seed()

    def user(self, name: str) -> User:
        return User.objects.get(email=f"{name}@example.com")

    def club(self, slug: str) -> Club:
        return Club.objects.get(slug=slug)


class SeedContentTests(SeedTestCase):
    def test_creates_all_static_data(self):
        self.assertEqual(self.result.users_created, len(PLAYERS) + 1)  # + manager
        self.assertEqual(self.result.clubs_created, len(CLUBS))
        self.assertEqual(self.result.courts_created, sum(len(c.courts) for c in CLUBS))
        self.assertEqual(self.result.skipped_conflicts, [])

    def test_players_can_log_in_with_seed_password(self):
        for player in PLAYERS:
            with self.subTest(player=player.email):
                self.assertTrue(User.objects.get(email=player.email).check_password(SEED_PASSWORD))

    def test_visibility_scenarios(self):
        expected = {
            "ana": {"ibirapuera-tennis-center", "lisboa-tennis-club"},
            "bruno": {"ibirapuera-tennis-center", "lisboa-tennis-club", "jardins-country-club"},
            "carla": {
                "ibirapuera-tennis-center", "lisboa-tennis-club",
                "jardins-country-club", "morumbi-racquet-club",
            },
            "diego": {"ibirapuera-tennis-center", "lisboa-tennis-club"},  # suspended
            "elena": {"ibirapuera-tennis-center", "lisboa-tennis-club"},  # expired
        }
        for name, slugs in expected.items():
            with self.subTest(player=name):
                visible = set(get_visible_clubs(self.user(name)).values_list("slug", flat=True))
                self.assertEqual(visible, slugs)

    def test_inactive_club_and_court_are_hidden(self):
        courts = get_bookable_courts(self.user("carla"))

        self.assertFalse(courts.filter(club__slug="old-town-courts").exists())
        self.assertFalse(courts.filter(name="Court 5 - Under renovation").exists())

    def test_reservation_blocks_its_slot(self):
        court = Court.objects.get(name="Court 1 - Guga")

        slots = get_courts_availability([court], AvailabilityWindow.for_date(TOMORROW))[court.id]

        starts = [slot.start.time() for slot in slots]
        self.assertNotIn(time(18), starts)  # ana's confirmed booking
        self.assertNotIn(time(19), starts)  # bruno's pending hold
        self.assertIn(time(20), starts)

    def test_morumbi_uses_90_minute_grid_and_is_closed_on_sunday(self):
        court = Court.objects.get(name="Court B")
        sunday = NOW.date() + timedelta(days=6)

        tomorrow_slots = get_courts_availability([court], AvailabilityWindow.for_date(TOMORROW))[court.id]
        sunday_slots = get_courts_availability([court], AvailabilityWindow.for_date(sunday))[court.id]

        self.assertEqual(tomorrow_slots[1].start.time(), time(9, 30))
        self.assertEqual(sunday_slots, [])

    def test_stale_hold_is_expired_by_the_expiry_service(self):
        expired = expire_stale_pending_reservations()

        self.assertEqual(expired, 1)
        self.assertTrue(
            Reservation.objects.filter(
                status=Reservation.Status.EXPIRED, notes__contains="stale hold"
            ).exists()
        )

    def test_soon_booking_triggers_match_reminder(self):
        result = send_upcoming_match_reminder_task()

        self.assertEqual(result["reservations"], 1)
        self.assertEqual(result["reminded"], 2)  # ana + her guest bruno

    def test_prices_are_computed(self):
        booking = Reservation.objects.get(notes__contains="evening singles with lighting")

        self.assertEqual(booking.total_price, Decimal("150.00"))  # 120 + 30 lighting


class SeedIdempotencyTests(SeedTestCase):
    def test_rerun_creates_no_duplicates(self):
        before = counts()

        result = seed()

        self.assertEqual(counts(), before)
        self.assertEqual(result.users_created + result.clubs_created + result.courts_created, 0)

    def test_rerun_keeps_manual_edits_to_static_data(self):
        court = Court.objects.get(name="Center Court")
        court.hourly_rate = Decimal("999.00")
        court.save(update_fields=["hourly_rate"])

        seed()

        court.refresh_from_db()
        self.assertEqual(court.hourly_rate, Decimal("999.00"))

    def test_rerun_keeps_manual_reservations(self):
        court = Court.objects.get(name="Court 3 - Indoor")
        start = datetime.combine(TOMORROW, time(15), tzinfo=SAO_PAULO)
        mine = Reservation.objects.create(
            court=court, primary_player=self.user("ana"),
            start_datetime=start, end_datetime=start + timedelta(hours=1),
        )

        seed()

        self.assertTrue(Reservation.objects.filter(pk=mine.pk).exists())

    def test_seed_booking_overlapping_a_manual_one_is_skipped(self):
        Reservation.objects.filter(notes__contains="evening singles").delete()
        court = Court.objects.get(name="Court 1 - Guga")
        start = datetime.combine(TOMORROW, time(18), tzinfo=SAO_PAULO)
        mine = Reservation.objects.create(
            court=court, primary_player=self.user("diego"),
            start_datetime=start, end_datetime=start + timedelta(hours=1),
            status=Reservation.Status.CONFIRMED,
        )

        result = seed()

        self.assertEqual(len(result.skipped_conflicts), 1)
        self.assertTrue(Reservation.objects.filter(pk=mine.pk).exists())

    def test_reset_recreates_and_clears_seed_courts(self):
        court = Court.objects.get(name="Court 3 - Indoor")
        start = datetime.combine(TOMORROW, time(15), tzinfo=SAO_PAULO)
        Reservation.objects.create(
            court=court, primary_player=self.user("ana"),
            start_datetime=start, end_datetime=start + timedelta(hours=1),
        )
        before = counts()

        result = seed(reset=True)

        self.assertEqual(result.clubs_created, len(CLUBS))
        after = counts()
        self.assertEqual(after["Reservation"], before["Reservation"] - 1)  # manual one gone
        self.assertFalse(Reservation.objects.exclude(notes__startswith=SEED_NOTE).exists())

    def test_reset_leaves_non_seed_users_alone(self):
        admin = User.objects.create_superuser(email="admin@example.com", password="admin123")

        seed(reset=True)

        self.assertTrue(User.objects.filter(pk=admin.pk).exists())


class SeedCommandTests(FrozenTimeMixin, TestCase):
    frozen_now = NOW

    def test_refuses_when_debug_is_off(self):
        with self.assertRaisesMessage(CommandError, "DEBUG=False"):
            call_command("seed_dev_data", stdout=StringIO())
        self.assertFalse(Club.objects.exists())

    def test_force_overrides_debug_guard(self):
        call_command("seed_dev_data", "--force", stdout=StringIO())

        self.assertEqual(Club.objects.count(), len(CLUBS))

    @override_settings(DEBUG=True)
    def test_prints_summary_and_login_guide(self):
        out = StringIO()

        call_command("seed_dev_data", stdout=out)

        output = out.getvalue()
        self.assertIn("Seed data ready", output)
        self.assertIn(f"password for all: {SEED_PASSWORD}", output)
        self.assertIn("carla@example.com", output)

    @override_settings(DEBUG=True)
    def test_reset_flag(self):
        call_command("seed_dev_data", stdout=StringIO())
        out = StringIO()

        call_command("seed_dev_data", "--reset", "-v", "0", stdout=out)

        self.assertIn("(reset)", out.getvalue())
        self.assertEqual(Club.objects.count(), len(CLUBS))
