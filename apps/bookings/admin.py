from django.contrib import admin

from .models import Reservation


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = (
        "court",
        "primary_player",
        "start_datetime",
        "end_datetime",
        "booking_type",
        "status",
        "includes_lighting",
        "total_price",
        "cancelled_at",
        "created_at",
    )
    list_filter = (
        "status",
        "booking_type",
        "includes_lighting",
        "court__club",
        "court",
        "start_datetime",
    )
    search_fields = (
        "primary_player__email",
        "court__name",
        "court__club__name",
        "notes",
    )
    raw_id_fields = ("court", "primary_player")
    filter_horizontal = ("guests",)
    readonly_fields = ("created_at", "updated_at", "total_price", "cancelled_at")
    date_hierarchy = "start_datetime"
    ordering = ("-start_datetime",)
