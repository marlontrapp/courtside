from django import forms
from django.contrib import admin

from .models import BookingRule, BookingRuleCourt, BookingRuleDayTemplate, BookingRuleSlot, Reservation
from .rules import RULE_KINDS, rule_summary


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


class BookingRuleForm(forms.ModelForm):
    class Meta:
        model = BookingRule
        fields = "__all__"
        help_texts = {"kind": "One of: " + ", ".join(RULE_KINDS)}


class _LinkInline(admin.TabularInline):
    extra = 0

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        club_path = {"court": "club", "day_template": "club", "slot": "template__club"}[self.link_field]
        field = formset.form.base_fields[self.link_field]
        if obj is not None:  # only the rule's club's courts / templates / slots
            field.queryset = field.queryset.filter(**{club_path: obj.club})
        return formset


class BookingRuleCourtInline(_LinkInline):
    model = BookingRuleCourt
    link_field = "court"
    verbose_name_plural = "Courts (none = every court)"


class BookingRuleDayTemplateInline(_LinkInline):
    model = BookingRuleDayTemplate
    link_field = "day_template"
    verbose_name_plural = "Slot configurations / day templates (none = any)"


class BookingRuleSlotInline(_LinkInline):
    model = BookingRuleSlot
    link_field = "slot"
    verbose_name_plural = "Single slots (none = any)"


@admin.register(BookingRule)
class BookingRuleAdmin(admin.ModelAdmin):
    form = BookingRuleForm
    list_display = ("name", "club", "kind", "summary", "is_active", "valid_from", "valid_until", "updated_at")
    list_filter = ("is_active", "kind", "club")
    search_fields = ("name", "club__name")
    readonly_fields = ("created_at", "updated_at")
    inlines = (BookingRuleCourtInline, BookingRuleDayTemplateInline, BookingRuleSlotInline)

    @admin.display(description="summary")
    def summary(self, rule: BookingRule) -> str:
        return rule_summary(rule)
