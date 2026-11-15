"""Reservation model with PostgreSQL exclusion-constraint overlap protection; booking rules."""

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Case, DateField, DateTimeField, Exists, F, Func, OuterRef, Q, TimeField, Value, When
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedUUIDModel


class TsTzRange(Func):
    """Wrap start/end datetimes as a PostgreSQL tstzrange."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class AtTimeZone(Func):
    """PostgreSQL timezone(zone, timestamptz): wall-clock timestamp in `zone`."""

    function = "timezone"
    output_field = DateTimeField()


class ReservationQuerySet(models.QuerySet):
    def involving(self, user):
        """Reservations where `user` is the primary player or a guest (no duplicates)."""
        as_guest = Reservation.guests.through.objects.filter(reservation=OuterRef("pk"), user=user)
        return self.filter(Q(primary_player=user) | Q(Exists(as_guest)))

    def with_state(self, now=None):
        """Annotate `state` (see Reservation.State) — mirrors Reservation.state_at()."""
        now = now or timezone.now()
        S, St = Reservation.Status, Reservation.State
        return self.annotate(
            state=Case(
                When(status=S.CANCELLED, then=Value(St.CANCELLED)),
                When(status=S.EXPIRED, then=Value(St.EXPIRED)),
                When(status=S.COMPLETED, then=Value(St.COMPLETED)),
                When(status=S.CONFIRMED, end_datetime__lte=now, then=Value(St.COMPLETED)),
                When(status=S.PENDING_PAYMENT, end_datetime__lte=now, then=Value(St.EXPIRED)),
                When(status=S.PENDING_PAYMENT, then=Value(St.PENDING_PAYMENT)),
                default=Value(St.UPCOMING),
                output_field=models.CharField(),
            )
        )

    def with_local_times(self):
        """Annotate `local_date`, `local_start_time`, `local_end_time` in each club's timezone."""
        zone = F("court__club__timezone")
        return self.annotate(
            local_date=Cast(AtTimeZone(zone, "start_datetime"), DateField()),
            local_start_time=Cast(AtTimeZone(zone, "start_datetime"), TimeField()),
            local_end_time=Cast(AtTimeZone(zone, "end_datetime"), TimeField()),
        )


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

    class State(models.TextChoices):
        """
        Lifecycle bucket derived from status + time. Nothing flips a played
        CONFIRMED booking to COMPLETED, so "completed"/"upcoming" must be
        derived from the clock rather than read from `status`.
        """

        UPCOMING = "upcoming", _("Upcoming")
        PENDING_PAYMENT = "pending_payment", _("Pending payment")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")

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
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancellation_reason = models.CharField(max_length=500, blank=True)

    objects = ReservationQuerySet.as_manager()

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

    def state_at(self, now=None) -> str:
        """Python twin of ReservationQuerySet.with_state() for a single object."""
        now = now or timezone.now()
        S, St = self.Status, self.State
        if self.status == S.CANCELLED:
            return St.CANCELLED
        if self.status == S.EXPIRED:
            return St.EXPIRED
        if self.status == S.COMPLETED:
            return St.COMPLETED
        finished = self.end_datetime <= now
        if self.status == S.CONFIRMED:
            return St.COMPLETED if finished else St.UPCOMING
        return St.EXPIRED if finished else St.PENDING_PAYMENT


class BookingRule(TimeStampedUUIDModel):
    """
    A condition a booking must meet, set by a club: *where* it applies and
    *what* it requires (the kind and its params).

    Where — the rule belongs to a club and is linked to any of:
      courts, day templates (slot configurations, e.g. "Workdays") and single
      slots of a template (e.g. Workdays 19:00–20:00). Nothing linked = the
      whole club. Each kind of link narrows the rule (court AND template AND
      slot), any of the linked ones within a kind: courts 1–2 + the 19:00 slot
      = that slot on those two courts only. A booking is covered when it's on
      a linked court, on a day its court uses a linked template, and covers a
      linked slot. valid_from / valid_until limit when the rule is in force.
      Linked courts, templates and slots can't be deleted while linked, so a
      rule never silently widens.

    What — a key of apps.bookings.rules.RULE_KINDS, e.g. "player_gender" with
    params {"allowed": ["FEMALE"]}. Each kind validates its params and checks
    bookings; new kinds need no migration.

    Rules are checked when a booking is made; changing or removing a rule
    never affects existing bookings.
    """

    club = models.ForeignKey("courts.Club", on_delete=models.CASCADE, related_name="booking_rules")
    name = models.CharField(max_length=100, help_text=_('Shown to players, e.g. "Women\'s evening".'))
    kind = models.CharField(max_length=50, help_text=_("What the rule requires — see the booking rule kinds."))
    params = models.JSONField(default=dict, blank=True, help_text=_("Settings of the kind."))
    courts = models.ManyToManyField(
        "courts.Court", through="BookingRuleCourt", blank=True, related_name="booking_rules",
        help_text=_("Leave empty for every court of the club."),
    )
    day_templates = models.ManyToManyField(
        "courts.DayTemplate", through="BookingRuleDayTemplate", blank=True, related_name="booking_rules",
        help_text=_("Slot configurations; leave empty for any."),
    )
    slots = models.ManyToManyField(
        "courts.DayTemplateSlot", through="BookingRuleSlot", blank=True, related_name="booking_rules",
        help_text=_("Single slots of day templates; leave empty for any slot."),
    )
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True, help_text=_("Last day the rule applies (inclusive)."))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["club__name", "name"]
        verbose_name = _("booking rule")
        verbose_name_plural = _("booking rules")
        indexes = [models.Index(fields=["club", "is_active"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True) | Q(valid_until__isnull=True) | Q(valid_until__gte=F("valid_from")),
                name="booking_rule_until_not_before_from",
                violation_error_message=_("valid_until can't be before valid_from."),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.club} · {self.name} ({self.kind})"

    def clean(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from .rules import RULE_KINDS  # rules imports this module

        if self.kind not in RULE_KINDS:
            raise ValidationError({"kind": _("Unknown kind; use one of: %s.") % ", ".join(RULE_KINDS)})
        try:
            self.params = RULE_KINDS[self.kind].validate_params(self.params)
        except DRFValidationError as exc:
            raise ValidationError({"params": str(exc.detail)}) from exc

    def applies_on(self, day: date) -> bool:
        """Active and within its date range on `day` (club-local)."""
        return (
            self.is_active
            and (self.valid_from is None or self.valid_from <= day)
            and (self.valid_until is None or day <= self.valid_until)
        )

    def covers(self, court_id, template_id, slot_ids) -> bool:
        """
        Its links cover a booking on court `court_id`, on a day that court uses
        template `template_id`, covering the template slots `slot_ids`. Needs
        courts, day_templates and slots prefetched to avoid queries per call.
        """
        courts = {court.pk for court in self.courts.all()}
        templates = {template.pk for template in self.day_templates.all()}
        slots = {slot.pk for slot in self.slots.all()}
        return (
            (not courts or court_id in courts)
            and (not templates or template_id in templates)
            and (not slots or not slots.isdisjoint(slot_ids))
        )


# Links of a rule. RESTRICT: a linked court / template / slot can't be deleted on
# its own (the rule would silently widen); deleting the club still works.


class BookingRuleCourt(models.Model):
    rule = models.ForeignKey(BookingRule, on_delete=models.CASCADE)
    court = models.ForeignKey("courts.Court", on_delete=models.RESTRICT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule", "court"], name="unique_booking_rule_court")]


class BookingRuleDayTemplate(models.Model):
    rule = models.ForeignKey(BookingRule, on_delete=models.CASCADE)
    day_template = models.ForeignKey("courts.DayTemplate", on_delete=models.RESTRICT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule", "day_template"], name="unique_booking_rule_day_template")]


class BookingRuleSlot(models.Model):
    rule = models.ForeignKey(BookingRule, on_delete=models.CASCADE)
    slot = models.ForeignKey("courts.DayTemplateSlot", on_delete=models.RESTRICT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule", "slot"], name="unique_booking_rule_slot")]
