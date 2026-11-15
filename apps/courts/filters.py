"""Query-string filters for court listings (django-filter)."""

from django_filters import rest_framework as filters

from apps.common.filters import ChoiceInFilter, UUIDInFilter

from .models import Court


class CourtFilter(filters.FilterSet):
    """All filters are combined with AND; multi-value params are OR within the param."""

    surface_type = ChoiceInFilter(choices=Court.SurfaceType.choices, lookup_expr="in", help_text="Court surfaces.")
    is_indoor = filters.BooleanFilter(help_text="Indoor (true) or outdoor (false) courts.")
    club = UUIDInFilter(field_name="club_id", lookup_expr="in", help_text="Club ids.")

    class Meta:
        model = Court
        fields = ["surface_type", "is_indoor", "club"]
