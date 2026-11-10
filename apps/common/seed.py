"""
Development seed data: clubs, courts, players, memberships, bookings and
one-time overrides (maintenance, a tournament) — each piece chosen to exercise a
feature you can test by hand.

Idempotent by design:
  * Clubs, courts, schedules, users, profiles and memberships are
    created only if missing — edits you make in the admin survive re-runs.
  * The *schedule* (reservations and one-time overrides tagged "[seed]")
    is regenerated relative to today on every run, so there is always upcoming activity to look at. Reservations you
    create yourself are never touched (seed bookings that would overlap one
    of yours are skipped).
  * reset=True first deletes everything the seed owns — including *all*
    reservations on seed courts — then recreates it.

Run via `python manage.py seed_dev_data [--reset]` or `make seed`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bookings.models import Reservation
from apps.bookings.services import calculate_total_price
from apps.courts.models import (
    Club,
    ClubMembership,
    ClubStaffRole,
    Court,
    CourtSchedule,
    CourtScheduleDay,
    DayTemplate,
    DayTemplateSlot,
    ScheduleOverride,
)
from apps.courts.schedule import day_slots, grid_slots
from apps.users.models import PlayerProfile, User

SEED_PASSWORD = "player123"
SCHEDULE_HISTORY_DAYS = 30  # seed schedules start this long ago, covering past seed bookings
SEED_NOTE = "[seed]"
MANAGER_EMAIL = "manager@example.com"

OPEN = Club.AccessType.OPEN
MEMBERS_ONLY = Club.AccessType.MEMBERS_ONLY
CLAY, HARD, GRASS, SYNTHETIC = (
    Court.SurfaceType.CLAY,
    Court.SurfaceType.HARD,
    Court.SurfaceType.GRASS,
    Court.SurfaceType.SYNTHETIC,
)
ACTIVE, SUSPENDED = ClubMembership.Status.ACTIVE, ClubMembership.Status.SUSPENDED
BASIC, PREMIUM, VIP = ClubMembership.Tier.BASIC, ClubMembership.Tier.PREMIUM, ClubMembership.Tier.VIP
R = Reservation.Status
T = Reservation.BookingType


@dataclass(frozen=True)
class CourtSpec:
    name: str
    surface: str
    indoor: bool = False
    lighting: bool = True
    rate: str = "100.00"
    lighting_fee: str = "0.00"
    active: bool = True


@dataclass(frozen=True)
class ClubSpec:
    key: str
    slug: str
    name: str
    access: str
    courts: tuple[CourtSpec, ...]
    tz: str = "America/Sao_Paulo"
    address: str = ""
    active: bool = True
    open_at: time = time(7)
    close_at: time = time(22)
    slot_minutes: int = 60
    days: tuple[int, ...] = tuple(range(7))  # 0 = Monday


CLUBS: tuple[ClubSpec, ...] = (
    ClubSpec(
        key="ibirapuera",
        slug="ibirapuera-tennis-center",
        name="Ibirapuera Tennis Center",
        access=OPEN,
        address="Av. Pedro Álvares Cabral, São Paulo - SP",
        courts=(
            CourtSpec("Court 1 - Guga", CLAY, rate="120.00", lighting_fee="30.00"),
            CourtSpec("Court 2 - Maria Esther", CLAY, rate="120.00", lighting_fee="30.00"),
            CourtSpec("Court 3 - Indoor", HARD, indoor=True, rate="150.00"),
            CourtSpec("Court 4 - Day Court", HARD, lighting=False, rate="90.00"),
            CourtSpec("Court 5 - Under renovation", CLAY, active=False),
        ),
    ),
    ClubSpec(
        key="jardins",
        slug="jardins-country-club",
        name="Jardins Country Club",
        access=MEMBERS_ONLY,
        address="Rua Estados Unidos, São Paulo - SP",
        open_at=time(6),
        close_at=time(23),
        courts=(
            CourtSpec("Center Court", CLAY, rate="180.00", lighting_fee="40.00"),
            CourtSpec("Grass Court", GRASS, rate="220.00", lighting_fee="40.00"),
            CourtSpec("Indoor Synthetic", SYNTHETIC, indoor=True, rate="160.00"),
        ),
    ),
    ClubSpec(
        key="morumbi",
        slug="morumbi-racquet-club",
        name="Morumbi Racquet Club",
        access=MEMBERS_ONLY,
        address="Av. Morumbi, São Paulo - SP",
        open_at=time(8),
        close_at=time(20),
        slot_minutes=90,  # 90-minute grid: 08:00, 09:30, ..., 18:30
        days=(0, 1, 2, 3, 4, 5),  # closed on Sundays
        courts=(
            CourtSpec("Court A", HARD, indoor=True, rate="200.00"),
            CourtSpec("Court B", CLAY, rate="170.00", lighting_fee="35.00"),
        ),
    ),
    ClubSpec(
        key="lisboa",
        slug="lisboa-tennis-club",
        name="Lisboa Tennis Club",
        access=OPEN,
        tz="Europe/Lisbon",  # slots come back with a +00:00/+01:00 offset
        address="Av. da Liberdade, Lisboa",
        open_at=time(8),
        close_at=time(21),
        courts=(
            CourtSpec("Campo 1", CLAY, rate="25.00", lighting_fee="5.00"),
            CourtSpec("Campo 2", HARD, rate="20.00", lighting_fee="5.00"),
        ),
    ),
    ClubSpec(
        key="oldtown",
        slug="old-town-courts",
        name="Old Town Courts (inactive)",
        access=OPEN,
        active=False,  # hidden from listings
        courts=(CourtSpec("Court 1", HARD),),
    ),
)


@dataclass(frozen=True)
class MembershipSpec:
    club: str
    status: str = ACTIVE
    tier: str = BASIC
    expires_in_days: int | None = None  # negative = already expired


@dataclass(frozen=True)
class PlayerSpec:
    email: str
    first_name: str
    last_name: str
    skill: str
    story: str
    memberships: tuple[MembershipSpec, ...] = ()


PLAYERS: tuple[PlayerSpec, ...] = (
    PlayerSpec("ana@example.com", "Ana", "Souza", "3.0",
               "no memberships: sees open clubs only"),
    PlayerSpec("bruno@example.com", "Bruno", "Lima", "4.0",
               "member of Jardins",
               (MembershipSpec("jardins", tier=PREMIUM),)),
    PlayerSpec("carla@example.com", "Carla", "Mendes", "4.5",
               "member of Jardins AND Morumbi (multi-club)",
               (MembershipSpec("jardins", tier=VIP),
                MembershipSpec("morumbi", expires_in_days=365))),
    PlayerSpec("diego@example.com", "Diego", "Rocha", "2.5",
               "SUSPENDED at Jardins: sees open clubs only",
               (MembershipSpec("jardins", status=SUSPENDED),)),
    PlayerSpec("elena@example.com", "Elena", "Costa", "3.5",
               "EXPIRED at Morumbi: sees open clubs only",
               (MembershipSpec("morumbi", expires_in_days=-1),)),
)


@dataclass
class SeedResult:
    users_created: int = 0
    clubs_created: int = 0
    courts_created: int = 0
    memberships_created: int = 0
    reservations_created: int = 0
    overrides_created: int = 0
    skipped_conflicts: list[str] = field(default_factory=list)


@transaction.atomic
def seed(*, reset: bool = False, now: datetime | None = None) -> SeedResult:
    now = now or timezone.now()
    result = SeedResult()
    if reset:
        delete_seed_data()

    manager = _ensure_user(result, MANAGER_EMAIL, "Club", "Manager")
    players = {
        spec.email: _ensure_user(result, spec.email, spec.first_name, spec.last_name, skill=spec.skill)
        for spec in PLAYERS
    }
    clubs, courts = _ensure_clubs(result)
    _ensure_admin_roles(manager, clubs)
    _ensure_memberships(result, players, clubs, now)
    _refresh_schedule(result, players, manager, clubs, courts, now)
    return result


def delete_seed_data() -> None:
    """Remove everything the seed owns (plus all reservations on seed courts)."""
    seed_emails = [MANAGER_EMAIL, *(p.email for p in PLAYERS)]
    seed_clubs = Club.objects.filter(slug__in=[c.slug for c in CLUBS])
    # Reservation FKs are PROTECT: clear them first.
    Reservation.objects.filter(court__club__in=seed_clubs).delete()
    Reservation.objects.filter(primary_player__email__in=seed_emails).delete()
    seed_clubs.delete()  # cascades courts, schedules, overrides, day templates, memberships
    User.objects.filter(email__in=seed_emails).delete()  # cascades profiles, memberships


# --- static data (create-if-missing) ----------------------------------------------


def _ensure_user(result: SeedResult, email, first_name, last_name, *, skill=None) -> User:
    user = User.objects.filter(email=email).first()
    if user is None:
        user = User.objects.create_user(
            email=email, password=SEED_PASSWORD, first_name=first_name, last_name=last_name
        )
        result.users_created += 1
    if skill is not None:
        PlayerProfile.objects.get_or_create(
            user=user,
            defaults={"skill_level": skill, "phone_number": "+55 11 90000-0000"},
        )
    return user


def _ensure_clubs(result: SeedResult) -> tuple[dict[str, Club], dict[tuple[str, str], Court]]:
    clubs: dict[str, Club] = {}
    courts: dict[tuple[str, str], Court] = {}
    for spec in CLUBS:
        club, created = Club.objects.get_or_create(
            slug=spec.slug,
            defaults={
                "name": spec.name,
                "access_type": spec.access,
                "timezone": spec.tz,
                "address": spec.address,
                "is_active": spec.active,
            },
        )
        result.clubs_created += created
        clubs[spec.key] = club
        for court_spec in spec.courts:
            court, created = Court.objects.get_or_create(
                club=club,
                name=court_spec.name,
                defaults={
                    "surface_type": court_spec.surface,
                    "is_indoor": court_spec.indoor,
                    "has_lighting": court_spec.lighting,
                    "hourly_rate": Decimal(court_spec.rate),
                    "lighting_fee": Decimal(court_spec.lighting_fee),
                    "is_active": court_spec.active,
                },
            )
            result.courts_created += created
            courts[(spec.key, court_spec.name)] = court
            # Schedule: one day template per club, used on the club's open
            # weekdays; only for courts without any schedule yet, so schedules
            # edited through the API survive re-seeding.
            if not court.schedules.exists():
                version = CourtSchedule.objects.create(
                    court=court, valid_from=timezone.localdate() - timedelta(days=SCHEDULE_HISTORY_DAYS)
                )
                CourtScheduleDay.objects.bulk_create(
                    CourtScheduleDay(schedule=version, weekday=day, day_template=_day_template(club, spec))
                    for day in spec.days
                )
    return clubs, courts


def _day_template(club: Club, spec) -> DayTemplate:
    name = f"{spec.open_at:%H:%M}–{spec.close_at:%H:%M}, {spec.slot_minutes} min"
    template, created = DayTemplate.objects.get_or_create(club=club, name=name)
    if created:
        DayTemplateSlot.objects.bulk_create(
            DayTemplateSlot(template=template, start_time=start, end_time=end)
            for start, end in grid_slots(spec.open_at, spec.close_at, spec.slot_minutes)
        )
    return template


def _ensure_admin_roles(manager: User, clubs) -> None:
    """The seed manager is a club admin of every seed club (to try the schedule API)."""
    for club in clubs.values():
        if not ClubStaffRole.objects.current().filter(user=manager, club=club, role=ClubStaffRole.Role.ADMIN).exists():
            ClubStaffRole.objects.create(
                user=manager, club=club, role=ClubStaffRole.Role.ADMIN,
                start_date=timezone.localdate() - timedelta(days=SCHEDULE_HISTORY_DAYS),
            )


def _ensure_memberships(result, players, clubs, now) -> None:
    for player in PLAYERS:
        for m in player.memberships:
            expires_at = None
            if m.expires_in_days is not None:
                expires_at = now + timedelta(days=m.expires_in_days)
            _, created = ClubMembership.objects.get_or_create(
                user=players[player.email],
                club=clubs[m.club],
                defaults={"status": m.status, "tier": m.tier, "expires_at": expires_at},
            )
            result.memberships_created += created


# --- schedule (regenerated relative to today) -------------------------------------


def _refresh_schedule(result, players, manager, clubs, courts, now) -> None:
    seed_courts = list(courts.values())
    Reservation.objects.filter(court__in=seed_courts, notes__startswith=SEED_NOTE).delete()
    ScheduleOverride.objects.filter(court__in=seed_courts, notes__startswith=SEED_NOTE).delete()

    p = players  # short alias for the table below
    ana, bruno, carla = p["ana@example.com"], p["bruno@example.com"], p["carla@example.com"]
    diego, elena = p["diego@example.com"], p["elena@example.com"]
    soon = _next_hour(now, courts[("ibirapuera", "Court 3 - Indoor")].club)

    # (club, court, day offset, start, end, player, status, type, lighting, guests, note)
    bookings = [
        ("ibirapuera", "Court 1 - Guga", 1, time(18), time(19), ana, R.CONFIRMED, T.SINGLES, True, [],
         "evening singles with lighting"),
        ("ibirapuera", "Court 1 - Guga", 1, time(19), time(20), bruno, R.PENDING_PAYMENT, T.SINGLES, True, [],
         "awaiting payment (expires after RESERVATION_HOLD_MINUTES)"),
        ("ibirapuera", "Court 2 - Maria Esther", 1, time(8), time(10), carla, R.CONFIRMED, T.DOUBLES, False,
         [ana, bruno, diego], "doubles with three guests"),
        ("ibirapuera", "Court 2 - Maria Esther", 2, time(17), time(18), diego, R.CANCELLED, T.SINGLES, False, [],
         "cancelled: does not block the slot"),
        ("ibirapuera", "Court 4 - Day Court", -1, time(10), time(11), bruno, R.COMPLETED, T.SINGLES, False, [],
         "played yesterday"),
        ("jardins", "Center Court", 1, time(10), time(11), bruno, R.CONFIRMED, T.SINGLES, False, [],
         "members-only club booking"),
        ("jardins", "Center Court", 1, time(11), time(13), carla, R.CONFIRMED, T.LESSON, False, [],
         "two-hour lesson"),
        ("jardins", "Grass Court", 2, time(9), time(10), carla, R.CONFIRMED, T.SINGLES, False, [elena],
         "member bringing a non-member guest"),
        ("morumbi", "Court A", 1, time(9, 30), time(11), carla, R.CONFIRMED, T.SINGLES, False, [],
         "90-minute grid slot"),
        ("lisboa", "Campo 1", 1, time(10), time(11), ana, R.CONFIRMED, T.SINGLES, False, [],
         "Lisbon timezone booking"),
    ]
    for club_key, court_name, day, start_t, end_t, player, status, btype, lights, guests, note in bookings:
        court = courts[(club_key, court_name)]
        start, end = _local(court.club, now, day, start_t), _local(court.club, now, day, end_t)
        _create_reservation(result, court, player, start, end, status, btype, lights, guests, note)

    # Starts within 2h -> picked up by the upcoming-match reminder task.
    court3 = courts[("ibirapuera", "Court 3 - Indoor")]
    _create_reservation(result, court3, ana, soon, soon + timedelta(hours=1), R.CONFIRMED, T.SINGLES,
                        False, [bruno], "starts soon: triggers the match reminder task")

    # Stale hold: created 30 min ago -> expired by the next expiry task run (every 5 min).
    court4 = courts[("ibirapuera", "Court 4 - Day Court")]
    stale = _create_reservation(
        result, court4, elena,
        _local(court4.club, now, 1, time(7)), _local(court4.club, now, 1, time(8)),
        R.PENDING_PAYMENT, T.SINGLES, False, [], "stale hold: the expiry task will expire it",
    )
    if stale is not None:
        Reservation.objects.filter(pk=stale.pk).update(created_at=now - timedelta(minutes=30))

    # One-time overrides: that day's slots minus the affected hours (or closed),
    # with a type and notes members see. Tagged "[seed]" so re-runs replace them.
    O = ScheduleOverride.Type
    # (club, courts, day offset, hours removed (None = all day), type, notes)
    overrides = [
        ("ibirapuera", ["Court 1 - Guga"], 2, (time(7), time(9)), O.MAINTENANCE,
         "Clay watering & rolling, no play until 09:00"),
        ("jardins", ["Grass Court"], 3, None, O.MAINTENANCE, "Net replacement (all day)"),
        ("lisboa", ["Campo 2"], 1, (time(14), time(16)), O.MAINTENANCE, "Line repainting, 14:00–16:00"),
        ("ibirapuera", ["Court 1 - Guga", "Court 2 - Maria Esther"], 4, (time(8), time(14)), O.TOURNAMENT,
         "Junior club championship until 14:00"),
    ]
    for club_key, court_names, day_offset, removed, kind, notes in overrides:
        for name in court_names:
            court = courts[(club_key, name)]
            day = _local(court.club, now, day_offset, time(12)).date()
            template = None
            if removed is not None:
                remaining = [slot for slot in day_slots(court, day) if not (slot[0] < removed[1] and slot[1] > removed[0])]
                template = _seed_template(court.club, f"No play {removed[0]:%H:%M}–{removed[1]:%H:%M}", remaining)
            ScheduleOverride.objects.update_or_create(
                court=court, date=day,
                defaults={"day_template": template, "type": kind, "notes": f"{SEED_NOTE} {notes}"},
            )
            result.overrides_created += 1


def _seed_template(club: Club, name: str, slots) -> DayTemplate:
    template, created = DayTemplate.objects.get_or_create(club=club, name=name)
    if created:
        DayTemplateSlot.objects.bulk_create(
            DayTemplateSlot(template=template, start_time=start, end_time=end) for start, end in slots
        )
    return template


def _create_reservation(result, court, player, start, end, status, btype, lights, guests, note):
    try:
        with transaction.atomic():  # savepoint: an overlap must not abort the whole seed
            reservation = Reservation.objects.create(
                court=court,
                primary_player=player,
                start_datetime=start,
                end_datetime=end,
                status=status,
                booking_type=btype,
                includes_lighting=lights,
                total_price=calculate_total_price(court, start, end, includes_lighting=lights),
                notes=f"{SEED_NOTE} {note}",
            )
            if guests:
                reservation.guests.set(guests)
    except IntegrityError:
        result.skipped_conflicts.append(f"{court} {start:%Y-%m-%d %H:%M} ({note})")
        return None
    result.reservations_created += 1
    return reservation


def _local(club: Club, now: datetime, day_offset: int, at: time) -> datetime:
    """`at` wall-clock time, `day_offset` days from today, in the club's timezone."""
    zone = ZoneInfo(club.timezone)
    day: date = now.astimezone(zone).date() + timedelta(days=day_offset)
    return datetime.combine(day, at, tzinfo=zone)


def _next_hour(now: datetime, club: Club) -> datetime:
    local = now.astimezone(ZoneInfo(club.timezone))
    return local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
