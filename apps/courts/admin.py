from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.utils.translation import gettext_lazy as _

from .models import (
    Club,
    ClubMembership,
    ClubStaffRole,
    Court,
    CourtSchedule,
    CourtScheduleDay,
    DayTemplate,
    DayTemplateSlot,
    ScheduleOverride,
)


class CourtInline(admin.TabularInline):
    model = Court
    extra = 0
    fields = (
        "name",
        "surface_type",
        "is_indoor",
        "has_lighting",
        "hourly_rate",
        "lighting_fee",
        "is_active",
    )
    show_change_link = True


class ClubMembershipInline(admin.TabularInline):
    model = ClubMembership
    extra = 0
    fields = ("user", "status", "tier", "expires_at")
    raw_id_fields = ("user",)


class ClubStaffRoleInlineFormSet(BaseInlineFormSet):
    """
    One active record per (user, role) at this club, checked on the whole
    submission. Needed because inline forms skip constraints that include the
    parent key (club), which would turn a duplicate into a database error.
    Validating the final state (the inline lists all of the club's roles)
    also allows deactivating a record and adding its successor in one save.
    """

    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE") or not data.get("is_active"):
                continue
            key = (data.get("user"), data.get("role"))
            if key in seen:
                form.add_error(None, constraint_message(ClubStaffRole, "unique_active_staff_role_per_user_club"))
            seen.add(key)


def constraint_message(model, name: str) -> str:
    return next(c for c in model._meta.constraints if c.name == name).violation_error_message


class ClubStaffRoleInline(admin.TabularInline):
    model = ClubStaffRole
    formset = ClubStaffRoleInlineFormSet
    extra = 0
    verbose_name_plural = _("staff roles (history: inactive records are kept)")
    fields = ("user", "role", "start_date", "end_date", "is_active")
    raw_id_fields = ("user",)


class ReadOnlyAdminMixin:
    """
    Schedules are changed through the API, which enforces the history and
    booking rules (no rewriting the past, conflicts with future bookings);
    the admin only shows them.
    """

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class CourtScheduleInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = CourtSchedule
    fields = ("valid_from", "valid_until")
    show_change_link = True
    extra = 0


class DayTemplateSlotInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = DayTemplateSlot
    fields = ("start_time", "end_time")
    extra = 0


class CourtScheduleDayInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = CourtScheduleDay
    fields = ("weekday", "day_template")
    extra = 0


@admin.register(Club)
class ClubAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "timezone",
        "access_type",
        "cancellation_window_hours",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "access_type", "timezone")
    search_fields = ("name", "slug", "address")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    inlines = (CourtInline, ClubMembershipInline, ClubStaffRoleInline)


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "club",
        "surface_type",
        "is_indoor",
        "has_lighting",
        "hourly_rate",
        "is_active",
    )
    list_filter = ("surface_type", "is_indoor", "has_lighting", "is_active", "club")
    search_fields = ("name", "club__name")
    readonly_fields = ("created_at", "updated_at")
    inlines = (CourtScheduleInline,)


@admin.register(DayTemplate)
class DayTemplateAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "club", "created_at")
    list_filter = ("club",)
    search_fields = ("name", "club__name")
    inlines = (DayTemplateSlotInline,)


@admin.register(CourtSchedule)
class CourtScheduleAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("court", "valid_from", "valid_until")
    list_filter = ("court__club",)
    search_fields = ("court__name", "court__club__name")
    inlines = (CourtScheduleDayInline,)


@admin.register(ScheduleOverride)
class ScheduleOverrideAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("court", "date", "type", "day_template", "notes")
    list_filter = ("type", "court__club")
    search_fields = ("court__name", "court__club__name", "notes")


@admin.register(ClubMembership)
class ClubMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "club", "status", "tier", "expires_at", "created_at")
    list_filter = ("status", "tier", "club")
    search_fields = ("user__email", "club__name")
    raw_id_fields = ("user", "club")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ClubStaffRole)
class ClubStaffRoleAdmin(admin.ModelAdmin):
    list_display = ("user", "club", "role", "start_date", "end_date", "is_active", "created_at")
    list_filter = ("role", "is_active", "club")
    search_fields = ("user__email", "club__name")
    raw_id_fields = ("user", "club")
    readonly_fields = ("created_at", "updated_at")
