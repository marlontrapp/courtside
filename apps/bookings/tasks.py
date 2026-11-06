"""Celery tasks for reservation lifecycle notifications and cleanup."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="apps.bookings.tasks.send_booking_confirmation_task")
def send_booking_confirmation_task(reservation_id: str) -> dict:
    """
    Simulate sending booking confirmation notifications (email / WhatsApp webhook).

    Replace the logging stubs with real providers when integrations are ready.
    """
    from .models import Reservation

    try:
        reservation = Reservation.objects.select_related(
            "court",
            "court__club",
            "primary_player",
        ).get(pk=reservation_id)
    except Reservation.DoesNotExist:
        logger.error("Confirmation skipped: reservation %s not found", reservation_id)
        return {"ok": False, "reason": "not_found"}

    player_email = reservation.primary_player.email
    logger.info(
        "Sending booking confirmation for reservation=%s to=%s "
        "court=%s start=%s end=%s status=%s",
        reservation.id,
        player_email,
        reservation.court,
        reservation.start_datetime.isoformat(),
        reservation.end_datetime.isoformat(),
        reservation.status,
    )
    # Placeholder channels
    logger.info("[email] Confirmation queued → %s", player_email)
    logger.info("[whatsapp] Confirmation webhook queued → player=%s", player_email)

    return {
        "ok": True,
        "reservation_id": str(reservation.id),
        "notified": player_email,
    }


@shared_task(name="apps.bookings.tasks.expire_unpaid_reservations_task")
def expire_unpaid_reservations_task() -> dict:
    """
    Periodic task (every 5 minutes): expire PENDING_PAYMENT holds older than
    RESERVATION_HOLD_MINUTES so the court slot is freed.
    """
    from .services import expire_stale_pending_reservations

    expired_count = expire_stale_pending_reservations()
    logger.info("Expired %s unpaid reservation(s)", expired_count)
    return {"expired": expired_count}


@shared_task(name="apps.bookings.tasks.send_upcoming_match_reminder_task")
def send_upcoming_match_reminder_task() -> dict:
    """
    Query CONFIRMED reservations starting within the next 2 hours and
    dispatch player reminders.
    """
    from .models import Reservation

    now = timezone.now()
    window_end = now + timedelta(hours=2)

    upcoming = (
        Reservation.objects.filter(
            status=Reservation.Status.CONFIRMED,
            start_datetime__gte=now,
            start_datetime__lte=window_end,
        )
        .select_related("primary_player", "court", "court__club")
        .prefetch_related("guests")
    )

    reminded = 0
    for reservation in upcoming:
        recipients = {reservation.primary_player.email}
        recipients.update(guest.email for guest in reservation.guests.all())
        for email in recipients:
            logger.info(
                "[reminder] Match reminder → %s | court=%s | starts=%s",
                email,
                reservation.court,
                reservation.start_datetime.isoformat(),
            )
            reminded += 1

    logger.info("Dispatched %s upcoming-match reminder(s)", reminded)
    return {"reminded": reminded, "reservations": upcoming.count()}
