"""
Accounts: sign up, log in with a password or with a code sent by email, and
reset the password. The password is optional — an account without one logs in
with codes (and can set one through the password reset).

Codes are 6 digits, emailed, valid for AUTH_CODE_TTL_MINUTES, AUTH_CODE_MAX_ATTEMPTS
tries, single use. Each request creates an EmailChallenge whose id is returned
and must come back with the code. Only a keyed hash of the code is stored.

No account enumeration: requesting a code or a password reset answers the
same whether the email has an account or not (only accounts get an email).
Signing up with an email that has an account doesn't say so either: its
owner gets a code that logs them in, and the same verify call completes.

The signup's account is only created once the code is verified, with the
name and (hashed) password of that signup request — never of another one:
someone signing up with your email can't set a password you'd then adopt.
Rate limits count requests for the address whether it has an account or not:
one code per AUTH_CODE_RESEND_SECONDS per email and purpose (a login code then
a password reset is fine), AUTH_CODE_MAX_PER_HOUR per email in all.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import EmailChallenge, PlayerProfile, User
from .tasks import send_code_email_task

Purpose = EmailChallenge.Purpose

_HASH_SALT = "courtcontrol.users.email-code"
_EMAIL_LOCK_NAMESPACE = 0x656D  # "em": pg_advisory_xact_lock(namespace, key)


class CodeRateLimited(Exception):
    """Too many codes requested for this email; retry after `retry_after` seconds."""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"Too many codes requested for this email. Try again in {self.retry_after} seconds.")


class InvalidCode(Exception):
    """Wrong, expired, used or exhausted code — str() is the message for the user."""


@dataclass(frozen=True)
class SignupResult:
    user: User
    created: bool  # False: the email already had an account, which is now logged in


def normalize_email(email: str) -> str:
    return email.strip().lower()


def active_user(email: str) -> User | None:
    return User.objects.filter(email__iexact=normalize_email(email), is_active=True).first()


# --- Requesting codes -----------------------------------------------------------------


def request_signup(*, email: str, first_name: str, last_name: str, password: str | None = None) -> EmailChallenge:
    """
    Start a signup: email a code. The account is created when it's verified
    (verify_signup) — with this request's names and password. Email already
    registered: its owner gets a login code instead (verify_signup logs them in).
    """
    email = normalize_email(email)
    existing = User.objects.filter(email__iexact=email).first()
    if existing is not None:
        return _new_challenge(email, Purpose.SIGNUP, kind="signup_existing" if existing.is_active else None)
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "password": make_password(password) if password else None,
    }
    return _new_challenge(email, Purpose.SIGNUP, kind="signup", payload=payload)


def request_login_code(email: str) -> EmailChallenge:
    email = normalize_email(email)
    return _new_challenge(email, Purpose.LOGIN, kind="login" if active_user(email) else None)


def request_password_reset(email: str) -> EmailChallenge:
    email = normalize_email(email)
    return _new_challenge(email, Purpose.PASSWORD_RESET, kind="password_reset" if active_user(email) else None)


@transaction.atomic
def _new_challenge(email: str, purpose: str, *, kind: str | None, payload: dict | None = None) -> EmailChallenge:
    """Create a challenge and, when `kind` is set, email its code (after commit). Rate-limited per email."""
    _lock_email(email)  # concurrent requests for one email: checked one at a time
    _check_rate(email, purpose)
    # A new code cancels the email's open ones for the same purpose: one valid code at a time.
    EmailChallenge.objects.filter(email=email, purpose=purpose, used_at__isnull=True).update(used_at=timezone.now())
    code = f"{secrets.randbelow(10**6):06d}"
    challenge = EmailChallenge(
        email=email, purpose=purpose, payload=payload or {},
        expires_at=timezone.now() + timedelta(minutes=settings.AUTH_CODE_TTL_MINUTES),
    )
    challenge.code_hash = _code_hash(challenge.pk, code)
    challenge.save()
    if kind is not None:
        transaction.on_commit(lambda: send_code_email_task.delay(email, code, kind))
    return challenge


def _check_rate(email: str, purpose: str) -> None:
    """One code per AUTH_CODE_RESEND_SECONDS per email and purpose; AUTH_CODE_MAX_PER_HOUR per email in all."""
    now = timezone.now()
    recent = list(
        EmailChallenge.objects.filter(email=email, created_at__gte=now - timedelta(hours=1))
        .order_by("created_at").values_list("created_at", "purpose")
    )
    same_purpose = [created for created, p in recent if p == purpose]
    if same_purpose and (elapsed := (now - same_purpose[-1]).total_seconds()) < settings.AUTH_CODE_RESEND_SECONDS:
        raise CodeRateLimited(settings.AUTH_CODE_RESEND_SECONDS - elapsed)
    if len(recent) >= settings.AUTH_CODE_MAX_PER_HOUR:
        raise CodeRateLimited((recent[0][0] + timedelta(hours=1) - now).total_seconds())


def _lock_email(email: str) -> None:
    key = int.from_bytes(hashlib.sha256(email.encode()).digest()[:4], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_EMAIL_LOCK_NAMESPACE, key])


def _code_hash(challenge_id, code: str) -> str:
    return salted_hmac(_HASH_SALT, f"{challenge_id}:{code}", algorithm="sha256").hexdigest()


# --- Verifying codes ------------------------------------------------------------------


_INVALID = "Invalid or expired code. Request a new one."


def _check(challenge_id, code: str, purpose: str) -> tuple[EmailChallenge | None, str | None]:
    """
    (the open challenge, None) if `code` is right — locked, call in a
    transaction; else (None, message). A wrong code uses up an attempt, saved
    here: the caller raises only after its transaction block has ended, so the
    attempt isn't rolled back with the error.
    """
    challenge = EmailChallenge.objects.select_for_update().filter(pk=challenge_id, purpose=purpose).first()
    if challenge is None or challenge.used_at is not None or challenge.expires_at <= timezone.now():
        return None, _INVALID
    if challenge.attempts >= settings.AUTH_CODE_MAX_ATTEMPTS:
        return None, "Too many wrong codes. Request a new one."
    if not constant_time_compare(_code_hash(challenge.pk, str(code).strip()), challenge.code_hash):
        challenge.attempts += 1
        challenge.save(update_fields=["attempts"])
        return None, _INVALID
    return challenge, None


def _verify(challenge_id, code: str, purpose: str, complete):
    """Check the code; if right, `complete(challenge)` in the same transaction. Raises InvalidCode."""
    with transaction.atomic():
        challenge, error = _check(challenge_id, code, purpose)
        if challenge is not None:
            return complete(challenge)
    raise InvalidCode(error)


def _use(challenge: EmailChallenge) -> None:
    """Mark it used, and every other open challenge of that email and purpose (old codes stop working)."""
    EmailChallenge.objects.filter(email=challenge.email, purpose=challenge.purpose, used_at__isnull=True).update(
        used_at=timezone.now()
    )


def verify_signup(challenge_id, code: str) -> SignupResult:
    def complete(challenge) -> SignupResult:
        existing = User.objects.filter(email__iexact=challenge.email).first()
        if existing is not None:  # registered meanwhile, or already before: the code proves it's theirs
            if not existing.is_active:
                raise InvalidCode(_INVALID)
            _use(challenge)
            return SignupResult(existing, created=False)
        if not challenge.payload:  # a signup for an email whose account was deleted since
            raise InvalidCode(_INVALID)
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=challenge.email, password=None,
                    first_name=challenge.payload["first_name"], last_name=challenge.payload["last_name"],
                )
        except IntegrityError:  # the same email verified at the same moment
            return SignupResult(User.objects.get(email__iexact=challenge.email), created=False)
        if challenge.payload.get("password"):
            user.password = challenge.payload["password"]  # already hashed
            user.save(update_fields=["password"])
        PlayerProfile.objects.create(user=user)
        _use(challenge)
        return SignupResult(user, created=True)

    return _verify(challenge_id, code, Purpose.SIGNUP, complete)


def verify_login_code(challenge_id, code: str) -> User:
    def complete(challenge) -> User:
        user = active_user(challenge.email)
        if user is None:
            raise InvalidCode(_INVALID)
        _use(challenge)
        return user

    return _verify(challenge_id, code, Purpose.LOGIN, complete)


def confirm_password_reset(challenge_id, code: str, new_password: str) -> User:
    """
    Set a new password (also for an account that had none). Every existing
    session of the user stops working (Django ties sessions to the password).
    The password is validated before the code is used up, so a weak one can
    be retried with the same code. Raises django ValidationError for it.
    """
    def complete(challenge) -> User:
        user = active_user(challenge.email)
        if user is None:
            raise InvalidCode(_INVALID)
        validate_password(new_password, user)
        user.set_password(new_password)
        user.save(update_fields=["password"])
        _use(challenge)
        return user

    return _verify(challenge_id, code, Purpose.PASSWORD_RESET, complete)


# --- Password login -------------------------------------------------------------------


def authenticate_password(email: str, password: str) -> User | None:
    user = active_user(email)
    if user is None:
        User().set_password(password)  # same work as a real check: timing doesn't reveal the email exists
        return None
    return user if user.check_password(password) else None


def validate_signup_password(password: str, *, email: str, first_name: str, last_name: str) -> None:
    """Django's password validators, against the would-be user's attributes. Raises ValidationError."""
    validate_password(password, User(email=normalize_email(email), first_name=first_name, last_name=last_name))
