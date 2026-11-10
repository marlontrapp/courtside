"""Query-string filters for "my reservations" (django-filter)."""

from functools import reduce
from operator import or_

from django import forms
from django.db.models import Exists, OuterRef, Q
from django_filters import rest_framework as filters

from apps.common.filters import CharInFilter, ChoiceInFilter, UUIDInFilter

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
    date = filters.DateFilter(field_name="local_date", input_formats=ISO_DATE)
    date_from = filters.DateFilter(field_name="local_date", lookup_expr="gte", input_formats=ISO_DATE)
    date_to = filters.DateFilter(field_name="local_date", lookup_expr="lte", input_formats=ISO_DATE)
    time_from = filters.TimeFilter(field_name="local_start_time", lookup_expr="gte")
    time_to = filters.TimeFilter(field_name="local_end_time", lookup_expr="lte")

    # What / where / who
    state = ChoiceInFilter(field_name="state", choices=Reservation.State.choices, lookup_expr="in")
    booking_type = ChoiceInFilter(choices=Reservation.BookingType.choices, lookup_expr="in")
    club = UUIDInFilter(field_name="court__club_id", lookup_expr="in")
    court = UUIDInFilter(field_name="court_id", lookup_expr="in")
    player = CharInFilter(method="filter_player", help_text="Emails; booker or guest.")
    role = filters.ChoiceFilter(
        choices=[("owner", "owner"), ("guest", "guest")], method="filter_role"
    )

    ordering = filters.OrderingFilter(
        fields=(("start_datetime", "start"), ("created_at", "created"), ("total_price", "price"))
    )

    class Meta:
        model = Reservation
        fields = []
        form = ReservationFilterForm

    def filter_player(self, queryset, name, emails):
        """Reservations in which any of the given players takes part (booker or guest)."""
        emails = [e.strip() for e in emails if e.strip()]
        if not emails:
            return queryset
        as_booker = reduce(or_, (Q(primary_player__email__iexact=e) for e in emails))
        as_guest = Reservation.guests.through.objects.filter(
            reduce(or_, (Q(user__email__iexact=e) for e in emails)),
            reservation=OuterRef("pk"),
        )
        return queryset.filter(as_booker | Q(Exists(as_guest)))

    def filter_role(self, queryset, name, role):
        user = self.request.user
        if role == "owner":
            return queryset.filter(primary_player=user)
        return queryset.exclude(primary_player=user)  # base queryset: guest otherwise
