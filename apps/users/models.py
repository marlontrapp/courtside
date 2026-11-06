"""Custom user and player profile models."""

import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils.translation import gettext_lazy as _


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
    """Extended tennis player profile linked 1:1 with User."""

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

    class MembershipTier(models.TextChoices):
        NONE = "NONE", "None"
        BASIC = "BASIC", "Basic"
        PREMIUM = "PREMIUM", "Premium"
        VIP = "VIP", "VIP"

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
    is_member = models.BooleanField(default=False)
    membership_tier = models.CharField(
        max_length=16,
        choices=MembershipTier.choices,
        default=MembershipTier.NONE,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("player profile")
        verbose_name_plural = _("player profiles")
        ordering = ["user__email"]

    def __str__(self) -> str:
        return f"{self.user.email} ({self.skill_level})"
