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

from apps.courts.models import Court, MaintenanceBlock, OperatingHours
from apps.users.models import User

from .models import Reservation
from .tasks import send_booking_confirmation_task

logger = logging.getLogger(__name__)


class BookingError(Exception):
    """Raised when a booking cannot be created."""


@dataclass(frozen=True)
class SlotValidationResult:
    is_valid: bool
    errors: list[str]

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

    club_tz = ZoneInfo(court.club.timezone)
    local_start = timezone.localtime(start, timezone=club_tz)
    day_of_week = local_start.weekday()  # Monday=0
    hours = OperatingHours.objects.filter(court=court, day_of_week=day_of_week).first()
    if hours is None:
        errors.append("Court has no operating hours for this day.")
    else:
        local_end = timezone.localtime(end, timezone=club_tz)
        if local_start.time() < hours.open_time or local_end.time() > hours.close_time:
            errors.append(
                f"Slot must fall within operating hours "
                f"({hours.open_time}–{hours.close_time})."
            )
        duration_minutes = int((end - start).total_seconds() // 60)
        if duration_minutes % hours.slot_duration_minutes != 0:
            errors.append(
                f"Slot duration must be a multiple of {hours.slot_duration_minutes} minutes."
            )

    maintenance_conflict = MaintenanceBlock.objects.filter(
        court=court,
        start_datetime__lt=end,
        end_datetime__gt=start,
    ).exists()
    if maintenance_conflict:
        errors.append("Court is under maintenance during the requested slot.")

    overlapping = Reservation.objects.filter(
        court=court,
        status__in=Reservation.ACTIVE_STATUSES,
        start_datetime__lt=end,
        end_datetime__gt=start,
    )
    if exclude_reservation_id:
        overlapping = overlapping.exclude(pk=exclude_reservation_id)
    if overlapping.exists():
        errors.append("Court already has an overlapping active reservation.")

    return SlotValidationResult(is_valid=not errors, errors=errors)


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
    """
    validation = validate_slot(court, start_datetime, end_datetime)
    if not validation.is_valid:
        raise BookingError("; ".join(validation.errors))

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
            "Court already has an overlapping active reservation."
        ) from exc

    if guests:
        reservation.guests.set(guests)

    if status == Reservation.Status.CONFIRMED:
        send_booking_confirmation_task.delay(str(reservation.id))

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
    send_booking_confirmation_task.delay(str(reservation.id))
    return reservation


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
