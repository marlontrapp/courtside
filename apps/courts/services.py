"""
Business logic for clubs: registration, which clubs/courts a user can see
and book, how users are assigned to (members-only) clubs, who administers a
club, and courts' weekly slot schedules.

Access rule
-----------
A user can see and book courts of a club when the club is active and either:
  * the club is OPEN, or
  * the club is MEMBERS_ONLY and the user holds an *active* membership
    (status ACTIVE and not expired).

A user can belong to any number of clubs. Anonymous users (None or
AnonymousUser) only ever see OPEN clubs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Club,
    ClubMembership,
    ClubStaffRole,
    Court,
    CourtSchedule,
    CourtScheduleDay,
    DayTemplate,
    DayTemplateSlot,
    ScheduleOverride,
    Weekday,
)
from .schedule import Slot, booking_error, club_today, format_slot, resolve_slots, validate_day_slots
from .tasks import send_club_decision_task, send_club_review_request_task

logger = logging.getLogger(__name__)


class MembershipError(Exception):
    """Raised when a membership operation is not allowed."""


# --- Club registration --------------------------------------------------------

SLUG_FALLBACK = "club"
_SLUG_MAX_LENGTH = Club._meta.get_field("slug").max_length
_SLUG_SUFFIX_ROOM = 8  # room for a "-<n>" suffix within the column length
_SLUG_CREATE_ATTEMPTS = 3


class ClubRegistrationLimit(Exception):
    """The user already has settings.CLUB_REGISTRATION_MAX_PENDING clubs awaiting approval. -> 429."""


class ClubReviewError(Exception):
    """An approval / rejection that doesn't apply to the club's state. -> 409."""


def register_club(*, name: str, address: str, registered_by=None) -> Club:
    """
    Create a club from the minimum registration data: name and address.

    Every other attribute (timezone, access type, cancellation window, ...)
    starts at its model default and is meant to be changed later by editing
    the club.

    Self-service: a club registered by a player (`registered_by`, not staff)
    starts PENDING — hidden from players until site staff approve it
    (approve_club) — and its registrant becomes its club admin, so they can
    set it up meanwhile. At most settings.CLUB_REGISTRATION_MAX_PENDING
    pending clubs per user (ClubRegistrationLimit). Registered by staff, or
    without a user (staff tools): approved right away.

    Club names need not be unique, but slugs are — see `unique_club_slug`.
    The slug is chosen with one lookup before inserting; the unique
    constraint stays the final guard, and a concurrent registration taking
    the same slug between the lookup and the insert is retried.
    """
    self_service = registered_by is not None and not registered_by.is_staff
    with transaction.atomic():
        if self_service:
            _check_pending_limit(registered_by)
        club = _create_club(name, address, registered_by)
        if self_service:
            ClubStaffRole.objects.create(
                user=registered_by, club=club, role=ClubStaffRole.Role.ADMIN, start_date=club_today(club)
            )
            transaction.on_commit(lambda: send_club_review_request_task.delay(str(club.pk)))
    return club


def _check_pending_limit(user) -> None:
    from apps.users.models import User  # users depends on courts

    User.objects.select_for_update().get(pk=user.pk)  # one registration per user at a time
    pending = Club.objects.filter(registered_by=user, approval_status=Club.ApprovalStatus.PENDING).count()
    if pending >= settings.CLUB_REGISTRATION_MAX_PENDING:
        raise ClubRegistrationLimit(
            f"You already have {pending} clubs awaiting approval; wait until they're reviewed."
        )


def _create_club(name: str, address: str, registered_by) -> Club:
    approved = registered_by is None or registered_by.is_staff
    status = Club.ApprovalStatus.APPROVED if approved else Club.ApprovalStatus.PENDING
    review = {"reviewed_by": registered_by, "reviewed_at": timezone.now()} if registered_by and approved else {}
    error: IntegrityError | None = None
    for _attempt in range(_SLUG_CREATE_ATTEMPTS):
        slug = unique_club_slug(name, address)
        try:
            # Savepoint: a slug collision must not break the outer (request) transaction.
            with transaction.atomic():
                return Club.objects.create(
                    name=name, address=address, slug=slug,
                    approval_status=status, registered_by=registered_by, **review,
                )
        except IntegrityError as exc:
            error = exc
    raise error


@transaction.atomic
def approve_club(club: Club, *, by) -> Club:
    """Make a pending (or rejected) club live; its registrant is emailed."""
    club = Club.objects.select_for_update().get(pk=club.pk)
    if club.approval_status == Club.ApprovalStatus.APPROVED:
        raise ClubReviewError("This club is already approved.")
    return _review(club, Club.ApprovalStatus.APPROVED, by=by, reason="")


@transaction.atomic
def reject_club(club: Club, *, by, reason: str) -> Club:
    """Reject a pending club (it stays hidden); its registrant is emailed the reason."""
    club = Club.objects.select_for_update().get(pk=club.pk)
    if club.approval_status != Club.ApprovalStatus.PENDING:
        raise ClubReviewError(
            "Only clubs awaiting approval can be rejected (to hide an approved club, deactivate it)."
        )
    if not reason.strip():
        raise ClubReviewError("Give the reason for the rejection: it's sent to whoever registered the club.")
    return _review(club, Club.ApprovalStatus.REJECTED, by=by, reason=reason.strip())


def _review(club: Club, status: str, *, by, reason: str) -> Club:
    club.approval_status, club.rejection_reason = status, reason
    club.reviewed_by, club.reviewed_at = by, timezone.now()
    club.save(update_fields=["approval_status", "rejection_reason", "reviewed_by", "reviewed_at", "updated_at"])
    transaction.on_commit(lambda: send_club_decision_task.delay(str(club.pk)))
    return club


def get_visible_club(user, pk) -> Club | None:
    """
    A club `user` may view: an approved one (any logged-in user; even if
    inactive), or a pending / rejected one they administer (or site staff).
    """
    club = Club.objects.filter(pk=pk).first()
    if club is None or club.is_approved:
        return club
    return club if get_administered_clubs(user).filter(pk=pk).exists() else None


def unique_club_slug(name: str, address: str) -> str:
    """
    First slug no existing club uses, trying in order:

      1. the name                            "tennis-club"
      2. the name + address                  "tennis-club-rua-augusta-100"
      3. the name + address + a number       "tennis-club-rua-augusta-100-2", "-3", ...

    All candidates are checked with a single query, however many are taken.
    """
    name_slug = slugify(name)[:_SLUG_MAX_LENGTH].strip("-") or SLUG_FALLBACK
    full_slug = (
        slugify(f"{name} {address}")[: _SLUG_MAX_LENGTH - _SLUG_SUFFIX_ROOM].strip("-")
        or name_slug[: _SLUG_MAX_LENGTH - _SLUG_SUFFIX_ROOM].strip("-")
        or SLUG_FALLBACK
    )
    taken = set(
        Club.objects.filter(
            Q(slug__in=[name_slug, full_slug]) | Q(slug__startswith=f"{full_slug}-")
        )
        .order_by()  # no need for the default ORDER BY name
        .values_list("slug", flat=True)
    )
    for candidate in (name_slug, full_slug):
        if candidate not in taken:
            return candidate
    suffix = 2
    while f"{full_slug}-{suffix}" in taken:
        suffix += 1
    return f"{full_slug}-{suffix}"


# --- Read side: visibility ---------------------------------------------------


def get_visible_clubs(user) -> QuerySet[Club]:
    """Active clubs the user may browse: all OPEN clubs + clubs they're a member of."""
    return Club.objects.active().accessible_to(user)


def get_bookable_courts(user, *, club: Club | None = None) -> QuerySet[Court]:
    """
    Active courts the user may book, across every club they can access.

    Pass `club` to narrow the listing to a single club.
    """
    courts = Court.objects.bookable().accessible_to(user).select_related("club")
    if club is not None:
        courts = courts.filter(club=club)
    return courts


def get_active_memberships(user) -> QuerySet[ClubMembership]:
    """The user's memberships that currently grant access, with their clubs."""
    return ClubMembership.objects.active().filter(user=user).select_related("club")


def has_active_membership(user, club: Club) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return ClubMembership.objects.active().filter(user=user, club=club).exists()


def can_access_club(user, club: Club) -> bool:
    """True if the user may see/book courts of `club` (ignores `is_active` flags)."""
    if not club.requires_membership:
        return True
    return has_active_membership(user, club)


def can_book_court(user, court: Court) -> bool:
    """True if the court is bookable and the user has access to its club."""
    if not (court.is_active and court.club.is_live):
        return False
    return can_access_club(user, court.club)


# --- Write side: membership management ---------------------------------------


@transaction.atomic
def assign_membership(
    *,
    user,
    club: Club,
    tier: str = ClubMembership.Tier.BASIC,
    expires_at: datetime | None = None,
) -> ClubMembership:
    """
    Grant `user` an active membership of `club`.

    Idempotent: if the user already has a membership of this club (in any
    status), it is reactivated and its tier/expiry are updated — the
    (user, club) pair stays unique.
    """
    if expires_at is not None and expires_at <= timezone.now():
        raise MembershipError("Membership expiry must be in the future.")

    membership, _created = ClubMembership.objects.update_or_create(
        user=user,
        club=club,
        defaults={
            "status": ClubMembership.Status.ACTIVE,
            "tier": tier,
            "expires_at": expires_at,
        },
    )
    return membership


def suspend_membership(membership: ClubMembership) -> ClubMembership:
    """Temporarily revoke access (e.g. unpaid dues). Reactivate via assign_membership."""
    return _transition(
        membership,
        to=ClubMembership.Status.SUSPENDED,
        allowed_from=(ClubMembership.Status.ACTIVE,),
    )


def cancel_membership(membership: ClubMembership) -> ClubMembership:
    """End the membership. The row is kept for history; re-join via assign_membership."""
    return _transition(
        membership,
        to=ClubMembership.Status.CANCELLED,
        allowed_from=(ClubMembership.Status.ACTIVE, ClubMembership.Status.SUSPENDED),
    )


def _transition(
    membership: ClubMembership,
    *,
    to: str,
    allowed_from: tuple[str, ...],
) -> ClubMembership:
    if membership.status not in allowed_from:
        raise MembershipError(
            f"Cannot change membership from {membership.status} to {to}."
        )
    membership.status = to
    membership.save(update_fields=["status", "updated_at"])
    return membership


# --- Administration --------------------------------------------------------------


def get_administered_clubs(user) -> QuerySet[Club]:
    """Every club (active or not) `user` administers."""
    if user is None or not getattr(user, "is_authenticated", False):
        return Club.objects.none()
    if user.is_staff:
        return Club.objects.all()
    return Club.objects.filter(
        pk__in=ClubStaffRole.objects.current().filter(user=user, role=ClubStaffRole.Role.ADMIN).values("club_id")
    )


def get_administered_courts(user) -> QuerySet[Court]:
    """Every court (active or not) of the clubs `user` administers."""
    if user is None or not getattr(user, "is_authenticated", False):
        return Court.objects.none()
    if user.is_staff:
        return Court.objects.all()
    admin_club_ids = ClubStaffRole.objects.current().filter(
        user=user, role=ClubStaffRole.Role.ADMIN
    ).values("club_id")
    return Court.objects.filter(club_id__in=admin_club_ids)


# --- Schedules: day templates, weekly versions, overrides --------------------------
#
# Every change goes through _apply(): lock the courts, note which future
# bookings fit the current slots, apply the change, re-check them. Bookings
# that fitted before and don't anymore make the change fail (ScheduleConflict,
# rolled back) — or, with force=True, are cancelled.
#
# History (day granularity, in each club's timezone): anything that applied
# to a day *before today* is never changed or deleted. Today is still
# editable (e.g. closing courts for rain), like any later day.


class ScheduleError(Exception):
    """An invalid schedule change (bad dates, templates, slots, history). -> 400."""

    def __init__(self, errors):
        self.errors = errors if isinstance(errors, (dict, list)) else [errors]
        super().__init__(str(self.errors))


class ScheduleInUse(ScheduleError):
    """The change would rewrite history or remove something still referenced. -> 409."""


class LinkedToRules(ScheduleInUse):
    """A court / day template / slot is linked to booking rules (and force wasn't set). -> 409."""

    def __init__(self, message: str, rules):
        self.rules = list(rules)
        super().__init__(message)


class ScheduleConflict(Exception):
    """Future bookings wouldn't fit the new slots (and force wasn't set). -> 409."""

    def __init__(self, reservations, message: str | None = None, hint: str | None = None):
        self.reservations = reservations
        self.hint = hint or "Pick a later start date, or send force=true to cancel these bookings."
        super().__init__(message or f"{len(reservations)} future booking(s) wouldn't fit the new schedule.")


@dataclass
class ScheduleChange:
    """
    Result of a schedule change: what was created/updated, bookings cancelled
    by force, and booking rules unlinked by force (apps.bookings.rules.UnlinkReport).
    """

    result: object = None
    cancelled: list = field(default_factory=list)
    rules: object = None


def _future_bookings(courts, date_from: date, date_to: date | None):
    """Active, not-yet-started reservations of `courts` on club-local dates in [date_from, date_to]."""
    from apps.bookings.models import Reservation  # bookings depends on courts

    bookings = []
    for reservation in (
        Reservation.objects.filter(
            court__in=courts, status__in=Reservation.ACTIVE_STATUSES, start_datetime__gte=timezone.now()
        )
        .select_related("court__club", "primary_player")
        .order_by("start_datetime")
    ):
        local_day = reservation.start_datetime.astimezone(ZoneInfo(reservation.court.club.timezone)).date()
        if local_day >= date_from and (date_to is None or local_day <= date_to):
            bookings.append(reservation)
    return bookings


def _fitting(bookings) -> set:
    """Ids of the bookings that fit their court's slots as currently resolved."""
    if not bookings:
        return set()
    tz = {b.pk: ZoneInfo(b.court.club.timezone) for b in bookings}
    local = {b.pk: (b.start_datetime.astimezone(tz[b.pk]), b.end_datetime.astimezone(tz[b.pk])) for b in bookings}
    days = [start.date() for start, _ in local.values()]
    resolved = resolve_slots({b.court for b in bookings}, min(days), max(days))
    return {
        b.pk
        for b in bookings
        if local[b.pk][0].date() == local[b.pk][1].date()
        and booking_error(resolved[b.court_id][local[b.pk][0].date()], local[b.pk][0].time(), local[b.pk][1].time()) is None
    }


@transaction.atomic
def _apply(courts, change, *, date_from: date, date_to: date | None = None, force: bool = False) -> ScheduleChange:
    from apps.bookings.models import Reservation

    courts = list(courts)
    list(Court.objects.select_for_update().filter(pk__in=[c.pk for c in courts]).order_by("pk"))
    bookings = _future_bookings(courts, date_from, date_to)
    fitted_before = _fitting(bookings)
    result = change()
    fitting_after = _fitting(bookings)
    broken = [b for b in bookings if b.pk in fitted_before and b.pk not in fitting_after]
    if broken and not force:
        raise ScheduleConflict(broken)  # rolls the change back
    if broken:
        Reservation.objects.filter(pk__in=[b.pk for b in broken]).update(
            status=Reservation.Status.CANCELLED, updated_at=timezone.now()
        )
        for booking in broken:
            booking.status = Reservation.Status.CANCELLED
        logger.info("Schedule change cancelled %d booking(s): %s", len(broken), [str(b.pk) for b in broken])
    return ScheduleChange(result=result, cancelled=broken)


def _check_slots(slots: list[Slot]) -> None:
    problems = validate_day_slots(slots)
    if problems:
        raise ScheduleError({"slots": problems})


def _check_templates(club: Club, templates) -> None:
    foreign = [t for t in templates if t is not None and t.club_id != club.pk]
    if foreign:
        raise ScheduleError({"day_template": "Day templates must belong to the court's club."})


def _check_not_past(club: Club, day: date, field_name: str) -> None:
    if day < club_today(club):
        raise ScheduleError({field_name: "Can't change the past: use today or a later date."})


# Day templates ----------------------------------------------------------------------


def template_locked(template: DayTemplate) -> bool:
    """True once the template has applied to a day before today (its slots are history)."""
    today = club_today(template.club)
    return (
        CourtScheduleDay.objects.filter(day_template=template, schedule__valid_from__lt=today).exists()
        or ScheduleOverride.objects.filter(day_template=template, date__lt=today).exists()
    )


@transaction.atomic
def create_day_template(*, club: Club, name: str, slots: list[Slot]) -> DayTemplate:
    _check_slots(slots)
    if DayTemplate.objects.filter(club=club, name__iexact=name).exists():
        raise ScheduleError({"name": "This club already has a day template with this name."})
    template = DayTemplate.objects.create(club=club, name=name)
    DayTemplateSlot.objects.bulk_create(
        DayTemplateSlot(template=template, start_time=start, end_time=end) for start, end in sorted(slots)
    )
    return template


def update_day_template(
    template: DayTemplate, *, name: str | None = None, slots: list[Slot] | None = None, force: bool = False
) -> ScheduleChange:
    """Rename freely; replace slots only while the template hasn't been in effect."""
    if name is not None and DayTemplate.objects.filter(club=template.club, name__iexact=name).exclude(pk=template.pk).exists():
        raise ScheduleError({"name": "This club already has a day template with this name."})
    if slots is not None:
        _check_slots(slots)
        if template_locked(template):
            raise ScheduleInUse(
                "This template has already been in effect, so its slots are part of the schedule "
                "history and can't change. Create a new template and schedule it from a date."
            )
        removed = [slot for slot in template.slots.all() if (slot.start_time, slot.end_time) not in set(slots)]
        if not force:
            _check_not_linked_to_rules(removed)
    unlinked = None

    def change():
        nonlocal unlinked
        if name is not None:
            template.name = name
            template.save(update_fields=["name", "updated_at"])
        if slots is not None:
            unlinked = _unlink(slots=removed)
            _replace_slots(template, slots)
        return template

    if slots is None:
        return ScheduleChange(result=change())
    users = Court.objects.filter(
        Q(schedules__days__day_template=template) | Q(schedule_overrides__day_template=template)
    ).distinct()
    result = _apply(users, change, date_from=club_today(template.club), force=force)
    result.rules = unlinked
    return result


def _replace_slots(template: DayTemplate, slots: list[Slot]) -> None:
    """
    Make the template's slots `slots`: unchanged slots are kept (with their ids,
    so booking rules linked to them stay linked), removed ones deleted, new ones added.
    """
    wanted = set(slots)
    existing = {(slot.start_time, slot.end_time): slot for slot in template.slots.all()}
    # Delete first: new slots may overlap removed ones (exclusion constraint).
    DayTemplateSlot.objects.filter(pk__in=[slot.pk for key, slot in existing.items() if key not in wanted]).delete()
    DayTemplateSlot.objects.bulk_create(
        DayTemplateSlot(template=template, start_time=start, end_time=end)
        for start, end in sorted(wanted - set(existing))
    )


_FORCE_UNLINK_HINT = (
    "Unlink it from the rule(s), or send force=true: the links are removed, and a rule left "
    "without any link of that kind is deactivated (rather than covering more)."
)


def _linked_rules(**links):
    from apps.bookings.rules import linked_rules  # bookings depends on courts

    return list(linked_rules(**links))


def _unlink(**links):
    from apps.bookings.rules import unlink_from_rules  # bookings depends on courts

    return unlink_from_rules(**links)


def _check_not_linked_to_rules(slots) -> None:
    """Slots linked to booking rules are only removed with force (see _FORCE_UNLINK_HINT)."""
    rules = _linked_rules(slots=slots) if slots else []
    if rules:
        linked = sorted({format_slot((s.start_time, s.end_time)) for s in slots if s.booking_rules.exists()})
        raise LinkedToRules(
            f"Slot(s) {', '.join(linked)} are linked to booking rule(s): {', '.join(r.name for r in rules)}.", rules
        )


@transaction.atomic
def delete_day_template(template: DayTemplate, *, force: bool = False):
    """
    Delete an unused template. Linked to booking rules (itself or its slots):
    LinkedToRules, or with force=True unlinked first — returns the UnlinkReport.
    """
    if CourtScheduleDay.objects.filter(day_template=template).exists() or template.overrides.exists():
        raise ScheduleInUse("This template is used by a schedule or an override, so it can't be deleted.")
    slots = list(template.slots.all())
    rules = _linked_rules(day_templates=[template], slots=slots)
    if rules and not force:
        raise LinkedToRules(
            f"This template is linked to booking rule(s): {', '.join(r.name for r in rules)}.", rules
        )
    report = _unlink(day_templates=[template], slots=slots) if rules else None
    template.delete()
    return report


# Courts (club admins and site staff) -------------------------------------------------


class CourtHasBookings(ScheduleInUse):
    """A court with bookings (history) can't be deleted — deactivate it instead. -> 409."""


def create_court(club: Club, **fields) -> Court:
    """A new court of `club` (pending clubs too: their admins set them up before approval)."""
    return Court.objects.create(club=club, **fields)


@transaction.atomic
def update_court(court: Court, fields: dict, *, force: bool = False) -> ScheduleChange:
    """
    Change a court's settings. Deactivating it with future bookings (not
    started, pending or confirmed) is refused — ScheduleConflict listing them
    — unless force=True, which cancels them (like schedule changes).
    Other changes never affect existing bookings (their price and lighting
    are kept; a court becoming singles-only keeps its doubles).
    """
    from apps.bookings.models import Reservation  # bookings depends on courts

    Court.objects.select_for_update().filter(pk=court.pk).first()
    cancelled = []
    if court.is_active and fields.get("is_active") is False:
        upcoming = _future_bookings([court], club_today(court.club), None)
        if upcoming and not force:
            raise ScheduleConflict(
                upcoming, f"{len(upcoming)} future booking(s) on this court: deactivating it would leave them "
                          "without a court.",
                hint="Send force=true to cancel these bookings, or deactivate the court after them.",
            )
        if upcoming:
            Reservation.objects.filter(pk__in=[b.pk for b in upcoming]).update(
                status=Reservation.Status.CANCELLED, updated_at=timezone.now()
            )
            for booking in upcoming:
                booking.status = Reservation.Status.CANCELLED
            cancelled = upcoming
            logger.info("Deactivating court %s cancelled %d booking(s)", court.pk, len(upcoming))
    for name, value in fields.items():
        setattr(court, name, value)
    court.save()
    return ScheduleChange(result=court, cancelled=cancelled)


@transaction.atomic
def delete_court(court: Court, *, force: bool = False):
    """
    Delete a court. One with bookings (any status: they're history) can't be
    — CourtHasBookings: deactivate it instead. Linked to booking rules:
    LinkedToRules, or with force=True unlinked first (a rule left without a
    court link is deactivated) — returns the UnlinkReport.
    """
    if court.reservations.exists():
        raise CourtHasBookings("This court has bookings (its history), so it can't be deleted: deactivate it instead.")
    rules = _linked_rules(courts=[court])
    if rules and not force:
        raise LinkedToRules(f"This court is linked to booking rule(s): {', '.join(r.name for r in rules)}.", rules)
    report = _unlink(courts=[court]) if rules else None
    court.delete()
    return report


# Weekly schedule versions -------------------------------------------------------------


def _days_of(version: CourtSchedule | None) -> dict[int, DayTemplate]:
    return {d.weekday: d.day_template for d in version.days.select_related("day_template")} if version else {}


def _create_version(court, valid_from, valid_until, days: dict[int, DayTemplate | None]) -> CourtSchedule:
    version = CourtSchedule.objects.create(court=court, valid_from=valid_from, valid_until=valid_until)
    CourtScheduleDay.objects.bulk_create(
        CourtScheduleDay(schedule=version, weekday=weekday, day_template=template)
        for weekday, template in sorted(days.items())
        if template is not None
    )
    return version


def _plan(court: Court, valid_from: date, valid_until: date | None, days: dict[int, DayTemplate | None]) -> CourtSchedule:
    """
    Put a new version on the court's timeline for [valid_from, valid_until]
    (no valid_until: until the next planned version, or open-ended).
    `days` may be partial: other weekdays keep the version in effect on
    valid_from. The version in effect on valid_from is cut short (or
    replaced, if it starts that same day); if it ran past valid_until, its
    remainder resumes afterwards.
    """
    versions = list(court.schedules.order_by("valid_from"))
    base = next((v for v in versions if v.covers(valid_from)), None)
    later_starts = [v.valid_from for v in versions if v.valid_from > valid_from]
    if valid_until is None:
        end = min(later_starts) - timedelta(days=1) if later_starts else None
    else:
        end = valid_until
        inside = sorted(start for start in later_starts if start <= end)
        if inside:
            raise ScheduleError({"valid_until": (
                "Other planned schedules start inside this period "
                f"({', '.join(str(d) for d in inside)}); delete them first or end this one before."
            )})

    base_days = _days_of(base)
    new_days = {**base_days, **days}
    if base is not None:
        old_until = base.valid_until
        if base.valid_from == valid_from:
            CourtSchedule.objects.filter(pk=base.pk).delete()  # a version starting the same day is replaced
        else:
            base.valid_until = valid_from - timedelta(days=1)
            base.save(update_fields=["valid_until", "updated_at"])
        if end is not None and (old_until is None or old_until > end):
            _create_version(court, end + timedelta(days=1), old_until, base_days)  # resume
    return _create_version(court, valid_from, end, new_days)


def plan_schedule(
    court: Court,
    *,
    valid_from: date,
    valid_until: date | None = None,
    days: dict[int, DayTemplate | None],
    force: bool = False,
) -> ScheduleChange:
    """Add a schedule version from `valid_from` (today or later). See _plan for the rules."""
    _check_not_past(court.club, valid_from, "valid_from")
    if valid_until is not None and valid_until < valid_from:
        raise ScheduleError({"valid_until": "Must not be before valid_from."})
    _check_templates(court.club, days.values())
    return _apply(
        [court], lambda: _plan(court, valid_from, valid_until, days),
        date_from=valid_from, date_to=valid_until, force=force,
    )


def copy_schedule(
    source: Court,
    targets: list[Court],
    *,
    valid_from: date,
    valid_until: date | None = None,
    force: bool = False,
) -> ScheduleChange:
    """
    Give each target court the weekly schedule the source has on
    `valid_from` (all seven weekdays, closed ones included), as a new version
    from valid_from. Same club only; all-or-nothing across the targets.
    """
    _check_not_past(source.club, valid_from, "valid_from")
    if valid_until is not None and valid_until < valid_from:
        raise ScheduleError({"valid_until": "Must not be before valid_from."})
    if any(target.club_id != source.club_id for target in targets):
        raise ScheduleError({"courts": "All courts must belong to the source court's club."})
    if any(target.pk == source.pk for target in targets):
        raise ScheduleError({"courts": "The source court can't be one of the targets."})
    version = next((v for v in source.schedules.all() if v.covers(valid_from)), None)
    if version is None:
        raise ScheduleError({"valid_from": "The source court has no schedule in effect on this date."})
    days = {weekday: None for weekday in Weekday.values} | _days_of(version)
    return _apply(
        targets, lambda: [_plan(target, valid_from, valid_until, days) for target in targets],
        date_from=valid_from, date_to=valid_until, force=force,
    )


def delete_schedule(version: CourtSchedule, *, force: bool = False) -> ScheduleChange:
    """
    Remove a version starting today or later; the version before it (if it
    ends the day before) extends over the freed period.
    """
    court = version.court
    if version.valid_from < club_today(court.club):
        raise ScheduleInUse("This schedule applied to past days, so it's part of the history and can't be deleted.")

    def change():
        previous = court.schedules.filter(valid_until=version.valid_from - timedelta(days=1)).first()
        # Queryset delete: leaves the caller's instance intact if a conflict rolls this back.
        CourtSchedule.objects.filter(pk=version.pk).delete()
        if previous is not None:
            previous.valid_until = version.valid_until
            previous.save(update_fields=["valid_until", "updated_at"])

    return _apply([court], change, date_from=version.valid_from, date_to=version.valid_until, force=force)


# One-time overrides -----------------------------------------------------------------------


def set_override(
    court: Court,
    *,
    day: date,
    day_template: DayTemplate | None,
    type: str = ScheduleOverride.Type.OTHER,
    notes: str = "",
    force: bool = False,
) -> ScheduleChange:
    """
    Create or replace the override for `day` (today or later): the day's
    slots become the template's (none = closed). `type` and `notes` tell
    members why — a tournament, maintenance, weather, …
    """
    _check_not_past(court.club, day, "date")
    _check_templates(court.club, [day_template])

    def change():
        override, _ = ScheduleOverride.objects.update_or_create(
            court=court, date=day, defaults={"day_template": day_template, "type": type, "notes": notes}
        )
        return override

    return _apply([court], change, date_from=day, date_to=day, force=force)


def delete_override(override: ScheduleOverride, *, force: bool = False) -> ScheduleChange:
    """Back to the weekly schedule for that date (today or later only)."""
    if override.date < club_today(override.court.club):
        raise ScheduleInUse("This override is in the past, so it's part of the history and can't be deleted.")
    return _apply(
        [override.court], ScheduleOverride.objects.filter(pk=override.pk).delete,
        date_from=override.date, date_to=override.date, force=force,
    )
