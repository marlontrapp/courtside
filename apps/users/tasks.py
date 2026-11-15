"""Celery tasks for accounts: emailing sign-up / login / password-reset codes, cleanup."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)

_MESSAGES = {
    "signup": (
        "Your CourtControl sign-up code",
        "Welcome to CourtControl! Your code to finish signing up is {code}.",
    ),
    "signup_existing": (
        "Your CourtControl login code",
        "Someone — probably you — tried to sign up with this email, but you already have an account. "
        "Your code to log in is {code}.",
    ),
    "login": ("Your CourtControl login code", "Your code to log in is {code}."),
    "password_reset": ("Reset your CourtControl password", "Your code to set a new password is {code}."),
}


@shared_task(name="apps.users.tasks.send_code_email_task")
def send_code_email_task(email: str, code: str, kind: str) -> None:
    """Email a one-time code (apps/users/auth.py). Returns nothing: the code isn't kept in task results."""
    subject, intro = _MESSAGES[kind]
    body = (
        f"{intro.format(code=code)}\n\n"
        f"It expires in {settings.AUTH_CODE_TTL_MINUTES} minutes. If you didn't ask for it, ignore this email."
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email])
    logger.info("Sent a %s code to %s", kind, email)


@shared_task(name="apps.users.tasks.purge_email_challenges_task")
def purge_email_challenges_task() -> int:
    """Delete challenges expired for over a day (kept that long for the hourly rate limit)."""
    from .models import EmailChallenge

    deleted, _ = EmailChallenge.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
    return deleted
