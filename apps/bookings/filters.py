"""Query-string filters for "my reservations" (django-filter)."""

from django import forms
from django.db.models import Exists, OuterRef, Q
from django_filters import rest_framework as filters

from apps.common.filters import CharInFilter, ChoiceInFilter, UUIDInFilter
from apps.users.references import parse_player_references

from .models import Reservation

ISO_DATE = ["%Y-%m-%d"]


class ReservationFilterForm(forms.Form):
    def clean(self):
        cleaned = super().clean()
        date_from, date_to = cleaned.get("date_from"), cleaned.get("date_to")
        if date_from and date_to and date_to < date_from:
            self.add_error("date_to", "Must not be before `date_from`.")
        time_from, time_to = cleaned.get("time_from"), cleaned.get("time_to")
        if time_from and time_to and time_to <= time_from:
            self.add_error("time_to", "Must be after `time_from`.")
        return cleaned


class ReservationFilter(filters.FilterSet):
    """
    Filters for the current user's reservations. Different params are
    AND-combined; comma-separated values within one param are OR-ed.
    Dates/times are wall-clock in each club's timezone. Requires a queryset
    annotated with `state` and local times (see ReservationQuerySet).
    """

    # When. ISO dates only: Django's locale formats would read 07/01/2030 as
    # July 1st (en-us), which is ambiguous for users writing DD/MM/YYYY.
    date = filters.DateFilter(field_name="local_date", input_formats=ISO_DATE, help_text="Bookings on that day.")
    date_from = filters.DateFilter(field_name="local_date", lookup_expr="gte", input_formats=ISO_DATE,
                                   help_text="On or after this day (inclusive; no range limit).")
    date_to = filters.DateFilter(field_name="local_date", lookup_expr="lte", input_formats=ISO_DATE,
                                 help_text="On or before this day (inclusive).")
    time_from = filters.TimeFilter(field_name="local_start_time", lookup_expr="gte",
                                   help_text="Starting at or after this time of day (HH:MM, club time).")
    time_to = filters.TimeFilter(field_name="local_end_time", lookup_expr="lte",
                                 help_text="Ending at or before this time of day (HH:MM, club time).")

    # What / where / who
    state = ChoiceInFilter(field_name="state", choices=Reservation.State.choices, lookup_expr="in",
                           help_text="Lifecycle states, derived from the status and the clock.")
    booking_type = ChoiceInFilter(choices=Reservation.BookingType.choices, lookup_expr="in", help_text="Booking types.")
    club = UUIDInFilter(field_name="court__club_id", lookup_expr="in", help_text="Club ids.")
    court = UUIDInFilter(field_name="court_id", lookup_expr="in", help_text="Court ids.")
    player = CharInFilter(method="filter_player", help_text="Player ids or emails (mixed); booker or guest.")
    role = filters.ChoiceFilter(
        choices=[("owner", "owner"), ("guest", "guest")], method="filter_role",
        help_text="What you are in the booking: the booker (owner) or a guest.",
    )

    ordering = filters.OrderingFilter(
        fields=(("start_datetime", "start"), ("created_at", "created"), ("total_price", "price"))
    )

    class Meta:
        model = Reservation
        fields = []
        form = ReservationFilterForm

    def filter_player(self, queryset, name, entries):
        """
        Reservations in which any of the given players takes part (booker or
        guest), each given by player id or email. Entries that are neither
        match nothing, like an unknown email.
        """
        if not any(entry.strip() for entry in entries):
            return queryset
        references = parse_player_references(entries)
        if not references:
            return queryset.none()
        as_guest = Reservation.guests.through.objects.filter(references.q("user"), reservation=OuterRef("pk"))
        return queryset.filter(references.q("primary_player") | Q(Exists(as_guest)))

    def filter_role(self, queryset, name, role):
        user = self.request.user
        if role == "owner":
            return queryset.filter(primary_player=user)
        return queryset.exclude(primary_player=user)  # base queryset: guest otherwise
