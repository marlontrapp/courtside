from django.contrib import admin

from .models import Club, Court, MaintenanceBlock, OperatingHours


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


class OperatingHoursInline(admin.TabularInline):
    model = OperatingHours
    extra = 0
    fields = ("day_of_week", "open_time", "close_time", "slot_duration_minutes")


@admin.register(Club)
class ClubAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "timezone",
        "cancellation_window_hours",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "timezone")
    search_fields = ("name", "slug", "address")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    inlines = (CourtInline,)


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
    inlines = (OperatingHoursInline,)


@admin.register(OperatingHours)
class OperatingHoursAdmin(admin.ModelAdmin):
    list_display = (
        "court",
        "day_of_week",
        "open_time",
        "close_time",
        "slot_duration_minutes",
    )
    list_filter = ("day_of_week", "court__club")
    search_fields = ("court__name", "court__club__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MaintenanceBlock)
class MaintenanceBlockAdmin(admin.ModelAdmin):
    list_display = (
        "court",
        "reason",
        "start_datetime",
        "end_datetime",
        "created_by",
        "created_at",
    )
    list_filter = ("court__club", "court", "start_datetime")
    search_fields = ("reason", "court__name", "created_by__email")
    raw_id_fields = ("court", "created_by")
    readonly_fields = ("created_at", "updated_at")
