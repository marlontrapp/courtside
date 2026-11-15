"""Custom user and player profile models."""

import uuid
from datetime import date

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.courts.models import Court

# Sanity floor for PlayerProfile.birth_date; "not in the future" is checked in
# clean() (a CHECK constraint can't depend on the current date).
MIN_BIRTH_DATE = date(1900, 1, 1)


class UserManager(BaseUserManager):
    """Manager for email-as-username authentication."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("The email address must be set.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """CourtControl user identified uniquely by email."""

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["email"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(_("email address"), unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    def __str__(self) -> str:
        return self.email


class PlayerProfile(models.Model):
    """
    Extended tennis player profile linked 1:1 with User.

    The player's name is the user's `first_name` / `last_name` (see
    `full_name`); it isn't duplicated here (only its normalized search copy
    is, maintained by the database). All player details are optional so a
    profile can be created before the player fills them in.

    Club membership isn't stored here: it is per club, in
    courts.ClubMembership (status, tier, expiry).
    """

    class SkillLevel(models.TextChoices):
        BEGINNER = "1.0", "1.0 – Beginner"
        NTRP_1_5 = "1.5", "1.5"
        NTRP_2_0 = "2.0", "2.0"
        NTRP_2_5 = "2.5", "2.5"
        NTRP_3_0 = "3.0", "3.0"
        NTRP_3_5 = "3.5", "3.5"
        NTRP_4_0 = "4.0", "4.0"
        NTRP_4_5 = "4.5", "4.5"
        NTRP_5_0 = "5.0", "5.0"
        NTRP_5_5 = "5.5", "5.5"
        PRO = "7.0", "7.0 – Tour / Pro"

    class Gender(models.TextChoices):
        FEMALE = "FEMALE", _("Female")
        MALE = "MALE", _("Male")
        OTHER = "OTHER", _("Other")
        PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY", _("Prefer not to say")

    class DominantHand(models.TextChoices):
        RIGHT = "RIGHT", _("Right-handed")
        LEFT = "LEFT", _("Left-handed")

    class BackhandStyle(models.TextChoices):
        ONE_HANDED = "ONE_HANDED", _("One-handed")
        TWO_HANDED = "TWO_HANDED", _("Two-handed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="player_profile",
    )
    phone_number = models.CharField(max_length=32, blank=True)
    skill_level = models.CharField(
        max_length=8,
        choices=SkillLevel.choices,
        default=SkillLevel.NTRP_3_0,
    )
    birth_date = models.DateField(null=True, blank=True)
    location = models.CharField(
        max_length=255,
        blank=True,
        help_text=_(
            'Where the player plays, free text: neighbourhood, city, state, country — '
            'e.g. "Pinheiros, São Paulo - SP, Brasil". Shown to other players and searchable: '
            "city level, no street address."
        ),
    )
    gender = models.CharField(
        max_length=32,
        choices=Gender.choices,
        blank=True,
        help_text=_(
            "Self-declared. Empty = not answered yet; \"Prefer not to say\" = chose not to answer."
        ),
    )
    dominant_hand = models.CharField(
        max_length=8,
        choices=DominantHand.choices,
        blank=True,
        help_text=_("The hand the player holds the racket with. Empty = not informed."),
    )
    backhand_style = models.CharField(
        max_length=16,
        choices=BackhandStyle.choices,
        blank=True,
        help_text=_("Empty = not informed."),
    )
    racket = models.CharField(
        max_length=100,
        blank=True,
        help_text=_('Brand and model, e.g. "Babolat Pure Aero 98".'),
    )
    preferred_surface = models.CharField(
        max_length=16,
        choices=Court.SurfaceType.choices,
        blank=True,
        help_text=_("Court surface the player prefers. Empty = no preference."),
    )
    # S3 allows object keys up to 1024 bytes.
    picture_key = models.CharField(
        max_length=1024,
        blank=True,
        help_text=_(
            "Key of the profile picture in the external file storage (S3, CDN, ...) — "
            "not a URL; the URL is built from the key by the storage in use. "
            "Empty = no picture."
        ),
    )
    # Player search (apps/users/search.py): normalized copies (accents removed,
    # lower case) of the user's name, and of name + location. They span two
    # tables, so database triggers (users migration 0004) keep them current —
    # on profile writes and on user name changes. Whatever Python writes here
    # is replaced; not meant to be read or set in Python.
    search_name = models.TextField(default="", editable=False)
    search_document = models.TextField(default="", editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("player profile")
        verbose_name_plural = _("player profiles")
        ordering = ["user__email"]
        indexes = [
            # Trigram index: serves LIKE '%term%' on the search document.
            GinIndex(fields=["search_document"], opclasses=["gin_trgm_ops"], name="player_search_document_trgm"),
        ]
        constraints = [
            # Choices aren't enforced by the database otherwise (e.g. objects.create).
            models.CheckConstraint(
                condition=Q(gender__in=["", "FEMALE", "MALE", "OTHER", "PREFER_NOT_TO_SAY"]),
                name="player_profile_valid_gender",
                violation_error_message=_("Unknown gender."),
            ),
            models.CheckConstraint(
                condition=Q(dominant_hand__in=["", "RIGHT", "LEFT"]),
                name="player_profile_valid_dominant_hand",
                violation_error_message=_("Unknown dominant hand."),
            ),
            models.CheckConstraint(
                condition=Q(backhand_style__in=["", "ONE_HANDED", "TWO_HANDED"]),
                name="player_profile_valid_backhand_style",
                violation_error_message=_("Unknown backhand style."),
            ),
            models.CheckConstraint(
                condition=Q(preferred_surface__in=["", *Court.SurfaceType.values]),
                name="player_profile_valid_preferred_surface",
                violation_error_message=_("Unknown court surface."),
            ),
            models.CheckConstraint(
                condition=Q(birth_date__isnull=True) | Q(birth_date__gte=MIN_BIRTH_DATE),
                name="player_profile_birth_date_after_1900",
                violation_error_message=_("The birth date can't be before 1900."),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} ({self.skill_level})"

    def clean(self):
        errors = {}
        if self.birth_date and self.birth_date > timezone.localdate():
            errors["birth_date"] = _("The birth date can't be in the future.")
        key = self.picture_key
        if key and ("://" in key or key.startswith("/")):
            errors["picture_key"] = _(
                'Store the storage key (e.g. "players/<id>/avatar.jpg"), not a URL or absolute path.'
            )
        if errors:
            raise ValidationError(errors)

    @property
    def full_name(self) -> str:
        """The player's name, from the user account ("" if not filled in)."""
        return self.user.get_full_name()

    def age(self, on: date | None = None) -> int | None:
        """Age in whole years on `on` (default: today in the project timezone); None without a birth date."""
        if self.birth_date is None:
            return None
        on = on or timezone.localdate()
        had_birthday = (on.month, on.day) >= (self.birth_date.month, self.birth_date.day)
        return on.year - self.birth_date.year - (0 if had_birthday else 1)


class EmailChallenge(models.Model):
    """
    A one-time code emailed to prove control of an address: to sign up, to log
    in without a password, or to reset the password (apps/users/auth.py).

    The id is returned to whoever asked and must be sent back with the code
    (so a signup's pending data can only be completed by its own requester).
    Only a keyed hash of the code is stored. A challenge is created even for
    an email without an account (nothing is sent then), so requests can't tell
    which emails are registered.
    """

    class Purpose(models.TextChoices):
        SIGNUP = "SIGNUP", _("Sign up")
        LOGIN = "LOGIN", _("Log in with a code")
        PASSWORD_RESET = "PASSWORD_RESET", _("Reset the password")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=False)  # lower-cased
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    code_hash = models.CharField(max_length=64)
    # Signup: the pending account (names, hashed password) — applied only once verified.
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("email challenge")
        verbose_name_plural = _("email challenges")
        indexes = [models.Index(fields=["email", "created_at"])]  # rate limits, invalidation
        constraints = [
            models.CheckConstraint(
                condition=Q(purpose__in=["SIGNUP", "LOGIN", "PASSWORD_RESET"]), name="email_challenge_valid_purpose"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.email} · {self.purpose} · {self.created_at:%Y-%m-%d %H:%M}"
