"""Club, court, schedule (day templates, weekly versions, overrides) and staff role models."""

from datetime import time
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Exists, Func, OuterRef, Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedUUIDModel


def _has_active_membership(user, club_ref: str) -> Exists:
    """EXISTS subquery: `user` holds a currently valid membership of the club at `club_ref`."""
    return Exists(
        ClubMembership.objects.active().filter(club=OuterRef(club_ref), user=user)
    )


def _is_anonymous(user) -> bool:
    return user is None or not getattr(user, "is_authenticated", False)


class ClubQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def accessible_to(self, user):
        """Open clubs, plus members-only clubs where `user` has an active membership."""
        is_open = Q(access_type=Club.AccessType.OPEN)
        if _is_anonymous(user):
            return self.filter(is_open)
        return self.filter(is_open | Q(_has_active_membership(user, "pk")))


class Club(TimeStampedUUIDModel):
    """Tennis club that owns one or more courts."""

    class AccessType(models.TextChoices):
        OPEN = "OPEN", _("Open to all players")
        MEMBERS_ONLY = "MEMBERS_ONLY", _("Members only")

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    timezone = models.CharField(max_length=64, default="America/Sao_Paulo")
    address = models.TextField(blank=True)
    cancellation_window_hours = models.PositiveIntegerField(
        default=24,
        help_text=_("Cancellation policy: players can cancel a confirmed booking up to this many hours before it starts. 0 = until the start time."),
    )
    is_active = models.BooleanField(default=True)
    access_type = models.CharField(
        max_length=16,
        choices=AccessType.choices,
        default=AccessType.OPEN,
        db_index=True,
        help_text=_("Members-only clubs restrict court visibility and booking to members."),
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="ClubMembership",
        related_name="clubs",
        blank=True,
    )

    objects = ClubQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = _("club")
        verbose_name_plural = _("clubs")

    def __str__(self) -> str:
        return self.name

    @property
    def requires_membership(self) -> bool:
        return self.access_type == self.AccessType.MEMBERS_ONLY

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class CourtQuerySet(models.QuerySet):
    def bookable(self):
        """Courts that are active and belong to an active club."""
        return self.filter(is_active=True, club__is_active=True)

    def accessible_to(self, user):
        """Courts of open clubs, plus members-only clubs where `user` is an active member."""
        is_open = Q(club__access_type=Club.AccessType.OPEN)
        if _is_anonymous(user):
            return self.filter(is_open)
        return self.filter(is_open | Q(_has_active_membership(user, "club")))


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

    objects = CourtQuerySet.as_manager()

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


class ClubMembershipQuerySet(models.QuerySet):
    def active(self, at=None):
        """Memberships that currently grant access: ACTIVE and not yet expired."""
        at = at or timezone.now()
        return self.filter(status=ClubMembership.Status.ACTIVE).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=at)
        )


class ClubMembership(TimeStampedUUIDModel):
    """Assignment of a user to a club. A user may belong to many clubs."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        SUSPENDED = "SUSPENDED", _("Suspended")
        CANCELLED = "CANCELLED", _("Cancelled")

    class Tier(models.TextChoices):
        BASIC = "BASIC", _("Basic")
        PREMIUM = "PREMIUM", _("Premium")
        VIP = "VIP", _("VIP")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="club_memberships",
    )
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="memberships")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    tier = models.CharField(max_length=16, choices=Tier.choices, default=Tier.BASIC)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Leave empty for a membership that does not expire."),
    )

    objects = ClubMembershipQuerySet.as_manager()

    class Meta:
        ordering = ["club__name", "user__email"]
        verbose_name = _("club membership")
        verbose_name_plural = _("club memberships")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "club"],
                name="unique_membership_per_user_club",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.club} [{self.status}]"

    def is_valid(self, at=None) -> bool:
        """True if this membership grants club access at `at` (default: now)."""
        at = at or timezone.now()
        return self.status == self.Status.ACTIVE and (
            self.expires_at is None or self.expires_at > at
        )


class ClubStaffRoleQuerySet(models.QuerySet):
    def current(self, on=None):
        """
        Roles in effect on `on` (a date; default: today in the project timezone):
        is_active, started on or before that day, and not yet ended (end_date
        is the last day the role applies; empty = no end).
        """
        on = on or timezone.localdate()
        return self.filter(is_active=True, start_date__lte=on).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=on)
        )


class ClubStaffRole(TimeStampedUUIDModel):
    """
    A role a user holds in running a club. Membership is not a role here — it
    lives in ClubMembership.

    Records are kept as history: a user can hold several different roles at
    the same club at once, but only one *active* record per role per club.
    To re-grant a role, deactivate the current record and add a new one.

    Roles describe responsibilities; the permissions they grant are not
    enforced yet.
    """

    class Role(models.TextChoices):
        # Full control over the club: courts, calendar blocks, overriding bookings, ...
        ADMIN = "ADMIN", _("Club admin")
        # Court upkeep: sees the schedule, marks courts under repair or
        # unplayable (weather, environment).
        GROUNDSKEEPER = "GROUNDSKEEPER", _("Groundskeeper")
        # On-site desk: books for in-person players, records no-shows, warns
        # players who break the rules.
        FRONT_DESK = "FRONT_DESK", _("Front desk")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="club_staff_roles",
    )
    # No single-column index: the (club, role) index covers lookups by club.
    club = models.ForeignKey(
        Club, on_delete=models.CASCADE, related_name="staff_roles", db_index=False
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("Last day the role applies. Leave empty for a role without an end."),
    )
    is_active = models.BooleanField(default=True)

    objects = ClubStaffRoleQuerySet.as_manager()

    class Meta:
        ordering = ["club__name", "user__email", "role", "-start_date"]
        verbose_name = _("club staff role")
        verbose_name_plural = _("club staff roles")
        indexes = [
            models.Index(fields=["club", "role"]),
        ]
        constraints = [
            # Partial unique index: inactive records are history and may repeat.
            models.UniqueConstraint(
                fields=["user", "club", "role"],
                condition=Q(is_active=True),
                name="unique_active_staff_role_per_user_club",
                violation_error_message=_(
                    "This user already has an active record of this role at this club."
                ),
            ),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="club_staff_role_end_not_before_start",
                violation_error_message=_("The end date can't be before the start date."),
            ),
            # Choices aren't enforced by the database otherwise (e.g. objects.create).
            models.CheckConstraint(
                condition=Q(role__in=["ADMIN", "GROUNDSKEEPER", "FRONT_DESK"]),
                name="club_staff_role_valid_role",
                violation_error_message=_("Unknown role."),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.club} [{self.role}]"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": _("The end date can't be before the start date.")})

    def is_current(self, on=None) -> bool:
        """True if this role is in effect on `on` (default: today)."""
        on = on or timezone.localdate()
        return (
            self.is_active
            and self.start_date <= on
            and (self.end_date is None or self.end_date >= on)
        )


class Weekday(models.IntegerChoices):
    """Python's numbering (date.weekday()): Monday = 0 … Sunday = 6."""

    MONDAY = 0, _("Monday")
    TUESDAY = 1, _("Tuesday")
    WEDNESDAY = 2, _("Wednesday")
    THURSDAY = 3, _("Thursday")
    FRIDAY = 4, _("Friday")
    SATURDAY = 5, _("Saturday")
    SUNDAY = 6, _("Sunday")


class WallClockRange(Func):
    """
    tsrange over two times of day, placed on a fixed dummy date so PostgreSQL
    can compare them as intervals (there is no built-in time range type).
    Half-open ('[)'): a slot ending at 07:00 doesn't overlap one starting at 07:00.
    """

    template = "TSRANGE(DATE '2000-01-01' + %(expressions)s, '[)')"
    arg_joiner = ", DATE '2000-01-01' + "
    output_field = DateTimeRangeField()


class InclusiveDateRange(Func):
    """daterange(from, until, '[]'); a NULL `until` is unbounded."""

    template = "DATERANGE(%(expressions)s, '[]')"
    output_field = DateRangeField()


class DayTemplate(TimeStampedUUIDModel):
    """
    A named set of slots for one day (e.g. "Workdays", "Weekend"), owned by a
    club and reused across its courts and weekdays by CourtSchedule and
    ScheduleOverride. Slot times are wall-clock in the club's timezone; a slot
    ending at midnight is stored as time.max (23:59:59.999999).

    Once a template has been in effect (used by a schedule that started, or
    an override for a past/current date), its slots are frozen so history
    stays accurate — change slots by scheduling a new template from a date.
    """

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="day_templates")
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["club__name", "name"]
        verbose_name = _("day template")
        verbose_name_plural = _("day templates")
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "club",
                name="unique_day_template_name_per_club",
                violation_error_message=_("This club already has a day template with this name."),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.club} · {self.name}"


class DayTemplateSlot(TimeStampedUUIDModel):
    """One slot of a day template: [start_time, end_time), never overlapping its siblings."""

    template = models.ForeignKey(DayTemplate, on_delete=models.CASCADE, related_name="slots")
    start_time = models.TimeField()
    end_time = models.TimeField(help_text=_("Midnight (24:00) is stored as 23:59:59.999999."))

    class Meta:
        ordering = ["template", "start_time"]
        verbose_name = _("day template slot")
        verbose_name_plural = _("day template slots")
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=models.F("start_time")),
                name="day_template_slot_end_after_start",
                violation_error_message=_("The end must be after the start."),
            ),
            ExclusionConstraint(
                name="exclude_overlapping_day_template_slots",
                expressions=[
                    ("template", RangeOperators.EQUAL),
                    (WallClockRange("start_time", "end_time"), RangeOperators.OVERLAPS),
                ],
                violation_error_message=_("This slot overlaps another slot of the template."),
            ),
        ]

    def __str__(self) -> str:
        end = "24:00" if self.end_time == time.max else f"{self.end_time:%H:%M}"
        return f"{self.template} {self.start_time:%H:%M}–{end}"


class CourtSchedule(TimeStampedUUIDModel):
    """
    One version of a court's weekly schedule, in effect from `valid_from` to
    `valid_until` (inclusive; empty = no end). A court's versions never
    overlap; together they are the history (and plan) of its schedule.
    Which template applies on each weekday is in `days` (no row = closed).
    """

    court = models.ForeignKey(Court, on_delete=models.CASCADE, related_name="schedules")
    valid_from = models.DateField()
    valid_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["court", "valid_from"]
        verbose_name = _("court schedule")
        verbose_name_plural = _("court schedules")
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gte=models.F("valid_from")),
                name="court_schedule_until_not_before_from",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_court_schedules",
                expressions=[
                    ("court", RangeOperators.EQUAL),
                    (InclusiveDateRange("valid_from", "valid_until"), RangeOperators.OVERLAPS),
                ],
                violation_error_message=_("This period overlaps another schedule of the court."),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.court} · from {self.valid_from} until {self.valid_until or '…'}"

    def covers(self, day) -> bool:
        return self.valid_from <= day and (self.valid_until is None or day <= self.valid_until)


class CourtScheduleDay(models.Model):
    """The day template a schedule version uses on one weekday."""

    schedule = models.ForeignKey(CourtSchedule, on_delete=models.CASCADE, related_name="days")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    # RESTRICT: a template in use can't be deleted on its own, but deleting
    # its club (which also deletes the courts using it) still works.
    day_template = models.ForeignKey(DayTemplate, on_delete=models.RESTRICT, related_name="schedule_days")

    class Meta:
        ordering = ["schedule", "weekday"]
        constraints = [
            models.UniqueConstraint(fields=["schedule", "weekday"], name="unique_weekday_per_court_schedule"),
            models.CheckConstraint(condition=Q(weekday__gte=0, weekday__lte=6), name="court_schedule_day_valid_weekday"),
        ]

    def __str__(self) -> str:
        return f"{self.schedule} · {self.get_weekday_display()}: {self.day_template.name}"


class ScheduleOverride(TimeStampedUUIDModel):
    """
    A one-time change for one court on one date: that day uses `day_template`
    instead of the weekly schedule, or is closed if it's empty. `type` and
    `notes` tell members why (a tournament, maintenance, weather, …): the
    slots the override removes simply aren't bookable that day.
    """

    class Type(models.TextChoices):
        MAINTENANCE = "MAINTENANCE", _("Maintenance")
        WEATHER = "WEATHER", _("Weather")
        TOURNAMENT = "TOURNAMENT", _("Tournament")
        EVENT = "EVENT", _("Event")
        HOLIDAY = "HOLIDAY", _("Holiday")
        OTHER = "OTHER", _("Other")

    court = models.ForeignKey(Court, on_delete=models.CASCADE, related_name="schedule_overrides")
    date = models.DateField()
    day_template = models.ForeignKey(
        DayTemplate, on_delete=models.RESTRICT, null=True, blank=True, related_name="overrides",
        help_text=_("Leave empty to close the court that day."),
    )
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.OTHER)
    notes = models.CharField(
        max_length=500, blank=True,
        help_text=_('Shown to members, e.g. "Junior club championship until 14:00".'),
    )

    class Meta:
        ordering = ["court", "date"]
        verbose_name = _("schedule override")
        verbose_name_plural = _("schedule overrides")
        constraints = [
            models.UniqueConstraint(fields=["court", "date"], name="unique_schedule_override_per_court_date"),
        ]

    def __str__(self) -> str:
        return f"{self.court} · {self.date}: {self.day_template.name if self.day_template else 'closed'}"
