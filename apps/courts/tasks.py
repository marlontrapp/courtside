"""Celery tasks for club registration reviews: emailing reviewers and registrants."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


@shared_task(name="apps.courts.tasks.send_club_review_request_task")
def send_club_review_request_task(club_id: str) -> None:
    """Tell the reviewers (settings.CLUB_REVIEW_NOTIFY_EMAILS) a club awaits approval."""
    from .models import Club

    recipients = settings.CLUB_REVIEW_NOTIFY_EMAILS
    club = Club.objects.select_related("registered_by").filter(pk=club_id).first()
    if not recipients or club is None:
        return
    who = club.registered_by
    registrant = f"{who.get_full_name() or '-'} <{who.email}>" if who else "-"
    send_mail(
        f"New club awaiting approval: {club.name}",
        f"{club.name}\n{club.address}\nRegistered by: {registrant}\n\n"
        "Approve or reject it: POST /api/v1/clubs/<id>/approve/ or /reject/, or in the Django admin "
        f"(clubs awaiting approval). Club id: {club.pk}",
        settings.DEFAULT_FROM_EMAIL,
        recipients,
    )


@shared_task(name="apps.courts.tasks.send_club_decision_task")
def send_club_decision_task(club_id: str) -> None:
    """Tell whoever registered the club it was approved or rejected."""
    from .models import Club

    club = Club.objects.select_related("registered_by").filter(pk=club_id).first()
    if club is None or club.registered_by is None:
        return
    if club.approval_status == Club.ApprovalStatus.APPROVED:
        subject, body = (f"{club.name} is approved", f"{club.name} is approved: players can now find and book it.")
    else:
        subject = f"{club.name} wasn't approved"
        body = f"{club.name} wasn't approved.\n\nReason: {club.rejection_reason}"
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [club.registered_by.email])
    logger.info("Sent the review decision for club %s (%s)", club.pk, club.approval_status)
