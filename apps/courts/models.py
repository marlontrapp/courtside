"""Club, court, operating hours, and maintenance models."""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedUUIDModel


class Club(TimeStampedUUIDModel):
    """Tennis club that owns one or more courts."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    timezone = models.CharField(max_length=64, default="America/Sao_Paulo")
    address = models.TextField(blank=True)
    cancellation_window_hours = models.PositiveIntegerField(
        default=24,
        help_text=_("Minimum hours before start time when a booking can still be cancelled."),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("club")
        verbose_name_plural = _("clubs")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Court(TimeStampedUUIDModel):
    """Bookable tennis court belonging to a club."""

    class SurfaceType(models.TextChoices):
        CLAY = "CLAY", _("Clay")
        HARD = "HARD", _("Hard")
        GRASS = "GRASS", _("Grass")
        SYNTHETIC = "SYNTHETIC", _("Synthetic")

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="courts")
    name = models.CharField(max_length=128, help_text=_('e.g. "Court 1 - Guga"'))
    surface_type = models.CharField(
        max_length=16,
        choices=SurfaceType.choices,
        default=SurfaceType.CLAY,
    )
    is_indoor = models.BooleanField(default=False)
    has_lighting = models.BooleanField(default=True)
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    lighting_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["club__name", "name"]
        verbose_name = _("court")
        verbose_name_plural = _("courts")
        constraints = [
            models.UniqueConstraint(
                fields=["club", "name"],
                name="unique_court_name_per_club",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.club.name} · {self.name}"


class OperatingHours(TimeStampedUUIDModel):
    """Weekly operating window for a court."""

    court = models.ForeignKey(Court, on_delete=models.CASCADE, related_name="operating_hours")
    day_of_week = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(6)],
        help_text=_("0 = Monday … 6 = Sunday"),
    )
    open_time = models.TimeField()
    close_time = models.TimeField()
    slot_duration_minutes = models.PositiveIntegerField(default=60)

    class Meta:
        ordering = ["court", "day_of_week", "open_time"]
        verbose_name = _("operating hours")
        verbose_name_plural = _("operating hours")
        constraints = [
            models.UniqueConstraint(
                fields=["court", "day_of_week"],
                name="unique_operating_hours_per_court_day",
            ),
            models.CheckConstraint(
                condition=models.Q(close_time__gt=models.F("open_time")),
                name="operating_hours_close_after_open",
            ),
        ]

    def __str__(self) -> str:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{self.court} · {days[self.day_of_week]} {self.open_time}-{self.close_time}"

    def clean(self):
        if self.close_time and self.open_time and self.close_time <= self.open_time:
            raise ValidationError({"close_time": _("Close time must be after open time.")})


class MaintenanceBlock(TimeStampedUUIDModel):
    """Period during which a court is unavailable for booking."""

    court = models.ForeignKey(Court, on_delete=models.CASCADE, related_name="maintenance_blocks")
    reason = models.CharField(
        max_length=255,
        help_text=_('e.g. "Clay watering & rolling", "Net replacement"'),
    )
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="maintenance_blocks_created",
    )

    class Meta:
        ordering = ["-start_datetime"]
        verbose_name = _("maintenance block")
        verbose_name_plural = _("maintenance blocks")
        indexes = [
            models.Index(fields=["court", "start_datetime", "end_datetime"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_datetime__gt=models.F("start_datetime")),
                name="maintenance_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.court} · {self.reason} ({self.start_datetime} → {self.end_datetime})"

    def clean(self):
        if self.end_datetime and self.start_datetime and self.end_datetime <= self.start_datetime:
            raise ValidationError({"end_datetime": _("End must be after start.")})
