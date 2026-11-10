"""python manage.py seed_dev_data [--reset] [--force]"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.common.seed import CLUBS, MANAGER_EMAIL, PLAYERS, SEED_PASSWORD, seed


class Command(BaseCommand):
    help = (
        "Create development seed data (clubs, courts, players, memberships, bookings, "
        "one-time overrides). Safe to re-run: static data is created only if missing and the "
        "schedule is regenerated relative to today."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete all seed-owned data (and every reservation on seed courts) first.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even when DEBUG is False (seed users have weak passwords).",
        )

    def handle(self, *args, reset: bool, force: bool, **options):
        if not settings.DEBUG and not force:
            raise CommandError(
                "Refusing to seed with DEBUG=False: seed users have a weak shared password. "
                "Use --force if this really is a throwaway environment."
            )

        result = seed(reset=reset)

        created = (
            f"{result.users_created} users, {result.clubs_created} clubs, "
            f"{result.courts_created} courts, {result.memberships_created} memberships"
        )
        self.stdout.write(self.style.SUCCESS(
            f"Seed data ready{' (reset)' if reset else ''}: created {created}; "
            f"schedule refreshed with {result.reservations_created} reservations and "
            f"{result.overrides_created} one-time overrides."
        ))
        for skipped in result.skipped_conflicts:
            self.stdout.write(self.style.WARNING(f"  skipped (overlaps an existing booking): {skipped}"))

        if options["verbosity"] >= 1:
            self._print_guide()

    def _print_guide(self):
        w = self.stdout.write
        w(f"\n  Players (password for all: {SEED_PASSWORD})")
        for player in PLAYERS:
            w(f"    {player.email:<20} {player.story}")
        w(f"    {MANAGER_EMAIL:<20} club admin of every seed club (schedule API)")
        w("\n  Clubs")
        for club in CLUBS:
            flags = [club.access.replace("_", "-").lower(), club.tz]
            if not club.active:
                flags.append("INACTIVE")
            count = len(club.courts)
            w(f"    {club.name:<28} {count} court{'' if count == 1 else 's'}  ({', '.join(flags)})")
        w("")
