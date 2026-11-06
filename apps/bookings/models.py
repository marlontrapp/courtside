"""Reservation model with PostgreSQL exclusion-constraint overlap protection."""

from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedUUIDModel


class TsTzRange(Func):
    """Wrap start/end datetimes as a PostgreSQL tstzrange."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Reservation(TimeStampedUUIDModel):
    """Court booking with DB-level non-overlap guarantees for active statuses."""

    class BookingType(models.TextChoices):
        SINGLES = "SINGLES", _("Singles")
        DOUBLES = "DOUBLES", _("Doubles")
        LESSON = "LESSON", _("Lesson")
        TOURNAMENT = "TOURNAMENT", _("Tournament")
        BLOCKED = "BLOCKED", _("Blocked")

    class Status(models.TextChoices):
        PENDING_PAYMENT = "PENDING_PAYMENT", _("Pending payment")
        CONFIRMED = "CONFIRMED", _("Confirmed")
        CANCELLED = "CANCELLED", _("Cancelled")
        COMPLETED = "COMPLETED", _("Completed")
        EXPIRED = "EXPIRED", _("Expired")

    ACTIVE_STATUSES = (Status.PENDING_PAYMENT, Status.CONFIRMED)

    court = models.ForeignKey(
        "courts.Court",
        on_delete=models.PROTECT,
        related_name="reservations",
    )
    primary_player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="primary_reservations",
    )
    guests = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="guest_reservations",
    )
    start_datetime = models.DateTimeField(db_index=True)
    end_datetime = models.DateTimeField(db_index=True)
    booking_type = models.CharField(
        max_length=16,
        choices=BookingType.choices,
        default=BookingType.SINGLES,
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING_PAYMENT,
        db_index=True,
    )
    includes_lighting = models.BooleanField(default=False)
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-start_datetime"]
        verbose_name = _("reservation")
        verbose_name_plural = _("reservations")
        indexes = [
            models.Index(fields=["court", "start_datetime", "end_datetime"]),
            models.Index(fields=["status", "start_datetime"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_datetime__gt=F("start_datetime")),
                name="reservation_end_after_start",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_active_reservations",
                expressions=[
                    ("court", RangeOperators.EQUAL),
                    (TsTzRange("start_datetime", "end_datetime"), RangeOperators.OVERLAPS),
                ],
                condition=Q(status__in=["PENDING_PAYMENT", "CONFIRMED"]),
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.court} · {self.start_datetime:%Y-%m-%d %H:%M}"
            f"–{self.end_datetime:%H:%M} [{self.status}]"
        )

    def clean(self):
        if self.end_datetime and self.start_datetime and self.end_datetime <= self.start_datetime:
            raise ValidationError({"end_datetime": _("End must be after start.")})

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES
