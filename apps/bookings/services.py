"""Business logic for slot validation and reservation creation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.courts.models import Court
from apps.courts.schedule import booking_error, describe_override, resolve
from apps.courts.services import can_access_club
from apps.users.models import User

from .models import Reservation
from .tasks import send_booking_cancellation_task, send_booking_confirmation_task

logger = logging.getLogger(__name__)


class BookingError(Exception):
    """
    Raised when a booking cannot be created.

    `errors` lists every reason; `conflict` is True when the slot is taken by
    another reservation (the API maps that to 409 Conflict).
    """

    def __init__(self, *errors: str, conflict: bool = False):
        self.errors = list(errors)
        self.conflict = conflict
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class SlotValidationResult:
    is_valid: bool
    errors: list[str]
    conflict: bool = False  # an overlapping active reservation exists

    @classmethod
    def ok(cls) -> SlotValidationResult:
        return cls(is_valid=True, errors=[])

    @classmethod
    def fail(cls, *errors: str) -> SlotValidationResult:
        return cls(is_valid=False, errors=list(errors))


def _duration_hours(start: datetime, end: datetime) -> Decimal:
    seconds = Decimal((end - start).total_seconds())
    return seconds / Decimal("3600")


def calculate_total_price(
    court: Court,
    start: datetime,
    end: datetime,
    *,
    includes_lighting: bool,
) -> Decimal:
    hours = _duration_hours(start, end)
    total = court.hourly_rate * hours
    if includes_lighting:
        total += court.lighting_fee * hours
    return total.quantize(Decimal("0.01"))


def validate_slot(
    court: Court,
    start: datetime,
    end: datetime,
    *,
    exclude_reservation_id=None,
) -> SlotValidationResult:
    """Validate that a proposed slot is bookable for the given court."""
    errors: list[str] = []

    if end <= start:
        errors.append("End datetime must be after start datetime.")

    if start < timezone.now():
        errors.append("Cannot book a slot in the past.")

    if not court.is_active:
        errors.append("Court is not active.")

    if not court.club.is_active:
        errors.append("Club is not active.")

    if end > start:
        errors.extend(_schedule_errors(court, start, end))

    overlapping = Reservation.objects.filter(
        court=court,
        status__in=Reservation.ACTIVE_STATUSES,
        start_datetime__lt=end,
        end_datetime__gt=start,
    )
    if exclude_reservation_id:
        overlapping = overlapping.exclude(pk=exclude_reservation_id)
    conflict = overlapping.exists()
    if conflict:
        errors.append("Court already has an overlapping active reservation.")

    return SlotValidationResult(is_valid=not errors, errors=errors, conflict=conflict)


def _schedule_errors(court: Court, start: datetime, end: datetime) -> list[str]:
    """
    A booking must cover one or more consecutive slots of the court's
    schedule that day (apps.courts.schedule.booking_error). Times are
    wall-clock in the club's timezone.
    """
    club_tz = ZoneInfo(court.club.timezone)
    local_start = timezone.localtime(start, timezone=club_tz)
    local_end = timezone.localtime(end, timezone=club_tz)
    if local_end.date() != local_start.date():
        return ["Bookings can't cross midnight."]
    day = local_start.date()
    resolved = resolve([court], day, day)
    error = booking_error(resolved.slots[court.pk][day], local_start.time(), local_end.time())
    if error is None:
        return []
    overrides = resolved.overrides[court.pk]
    if overrides:  # tell the player why the day is different
        error = f"{error.rstrip('.')} — {describe_override(overrides[0])}."
    return [error]


@transaction.atomic
def create_reservation(
    *,
    court: Court,
    primary_player: User,
    start_datetime: datetime,
    end_datetime: datetime,
    booking_type: str = Reservation.BookingType.SINGLES,
    includes_lighting: bool = False,
    guests: list[User] | None = None,
    notes: str = "",
    status: str = Reservation.Status.PENDING_PAYMENT,
) -> Reservation:
    """
    Validate and create a reservation.

    Relies on the PostgreSQL ExclusionConstraint as the final concurrency
    backstop; IntegrityError is translated into a BookingError.

    Members-only clubs: the primary player must hold an active membership.
    Guests are not checked (members may bring non-member guests).
    """
    if not can_access_club(primary_player, court.club):
        raise BookingError("This court is restricted to club members.")

    validation = validate_slot(court, start_datetime, end_datetime)
    if not validation.is_valid:
        raise BookingError(*validation.errors, conflict=validation.conflict)

    if includes_lighting and not court.has_lighting:
        raise BookingError("This court does not support lighting.")

    total_price = calculate_total_price(
        court,
        start_datetime,
        end_datetime,
        includes_lighting=includes_lighting,
    )

    try:
        reservation = Reservation.objects.create(
            court=court,
            primary_player=primary_player,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            booking_type=booking_type,
            status=status,
            includes_lighting=includes_lighting,
            total_price=total_price,
            notes=notes,
        )
    except IntegrityError as exc:
        logger.warning(
            "Overlap constraint rejected reservation for court=%s %s–%s",
            court.id,
            start_datetime,
            end_datetime,
        )
        raise BookingError(
            "Court already has an overlapping active reservation.", conflict=True
        ) from exc

    if guests:
        reservation.guests.set(guests)

    if status == Reservation.Status.CONFIRMED:
        _enqueue_confirmation(reservation)

    return reservation


def confirm_reservation(reservation: Reservation) -> Reservation:
    """Mark a pending reservation as confirmed and enqueue notifications."""
    if reservation.status != Reservation.Status.PENDING_PAYMENT:
        raise BookingError(
            f"Only PENDING_PAYMENT reservations can be confirmed "
            f"(current={reservation.status})."
        )

    reservation.status = Reservation.Status.CONFIRMED
    reservation.save(update_fields=["status", "updated_at"])
    _enqueue_confirmation(reservation)
    return reservation


def _enqueue_confirmation(reservation: Reservation) -> None:
    """
    Send the confirmation only after the surrounding transaction commits:
    the worker must be able to read the reservation (with ATOMIC_REQUESTS the
    whole request is one transaction), and a rolled-back booking must not
    notify anyone. robust=True: a broker outage is logged instead of turning
    an already-committed booking into a server error.
    """
    reservation_id = str(reservation.id)
    transaction.on_commit(
        lambda: send_booking_confirmation_task.delay(reservation_id),
        robust=True,
    )


def expire_stale_pending_reservations(
    *,
    older_than_minutes: int | None = None,
) -> int:
    """Expire PENDING_PAYMENT holds older than the configured hold window."""
    hold_minutes = older_than_minutes or settings.RESERVATION_HOLD_MINUTES
    cutoff = timezone.now() - timedelta(minutes=hold_minutes)
    updated = Reservation.objects.filter(
        status=Reservation.Status.PENDING_PAYMENT,
        created_at__lt=cutoff,
    ).update(status=Reservation.Status.EXPIRED, updated_at=timezone.now())
    return updated


# --- Cancellation --------------------------------------------------------------


class CancellationError(Exception):
    """
    Raised when a reservation can't be cancelled.

    code: "forbidden"   — the user isn't the player who booked it
          "not_allowed" — wrong state, already started, or past the club's deadline
    """

    def __init__(self, message: str, *, code: str):
        self.code = code
        super().__init__(message)


def cancellation_deadline(reservation: Reservation) -> datetime:
    """Latest moment a confirmed booking can be cancelled, per its club's policy."""
    window = timedelta(hours=reservation.court.club.cancellation_window_hours)
    return reservation.start_datetime - window


def check_cancellation(reservation: Reservation, user, *, now: datetime | None = None) -> None:
    """
    Raise CancellationError unless `user` may cancel `reservation` now.
    Single source of truth for the policy (also drives the API's `can_cancel`).

    * Only the primary player (booker) can cancel; guests can't.
    * CONFIRMED: until start − club.cancellation_window_hours (0 = until start).
    * PENDING_PAYMENT: any time before the start (no payment taken yet).
    * Cancelled/expired/completed or already started bookings: never.
    """
    now = now or timezone.now()
    S = Reservation.Status
    if reservation.primary_player_id != getattr(user, "pk", None):
        raise CancellationError(
            "Only the player who made the booking can cancel it.", code="forbidden"
        )
    if reservation.status == S.CANCELLED:
        raise CancellationError("This booking is already cancelled.", code="not_allowed")
    if reservation.status not in Reservation.ACTIVE_STATUSES:
        raise CancellationError(
            f"A {reservation.get_status_display().lower()} booking can't be cancelled.",
            code="not_allowed",
        )
    if now >= reservation.start_datetime:
        raise CancellationError(
            "This booking has already started or finished.", code="not_allowed"
        )
    if reservation.status == S.CONFIRMED:
        deadline = cancellation_deadline(reservation)
        if now > deadline:
            club = reservation.court.club
            local_deadline = timezone.localtime(deadline, ZoneInfo(club.timezone))
            raise CancellationError(
                f"The cancellation deadline has passed: {club.name} allows cancelling up to "
                f"{club.cancellation_window_hours} hours before the start "
                f"(deadline was {local_deadline:%Y-%m-%d %H:%M}).",
                code="not_allowed",
            )


def can_cancel(reservation: Reservation, user, *, now: datetime | None = None) -> bool:
    try:
        check_cancellation(reservation, user, now=now)
    except CancellationError:
        return False
    return True


@transaction.atomic
def cancel_reservation(reservation: Reservation, *, user, reason: str = "") -> Reservation:
    """
    Cancel `reservation` on behalf of `user`, freeing the slot.

    Idempotent: cancelling a booking this user already cancelled returns it
    unchanged (no second notification), so client retries are safe. The row is
    locked so concurrent cancel requests can't both pass the policy check.
    """
    locked = (
        Reservation.objects.select_for_update(of=("self",))
        .select_related("court__club")
        .get(pk=reservation.pk)
    )
    if locked.status == Reservation.Status.CANCELLED and locked.primary_player_id == user.pk:
        return locked

    check_cancellation(locked, user)

    locked.status = Reservation.Status.CANCELLED
    locked.cancelled_at = timezone.now()
    locked.cancellation_reason = reason
    locked.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])

    reservation_id = str(locked.id)
    transaction.on_commit(
        lambda: send_booking_cancellation_task.delay(reservation_id),
        robust=True,
    )
    return locked
