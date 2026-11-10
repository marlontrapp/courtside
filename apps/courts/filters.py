"""Query-string filters for court listings (django-filter)."""

from django_filters import rest_framework as filters

from apps.common.filters import ChoiceInFilter, UUIDInFilter

from .models import Court


class CourtFilter(filters.FilterSet):
    """All filters are combined with AND; multi-value params are OR within the param."""

    surface_type = ChoiceInFilter(choices=Court.SurfaceType.choices, lookup_expr="in")
    is_indoor = filters.BooleanFilter()
    club = UUIDInFilter(field_name="club_id", lookup_expr="in")

    class Meta:
        model = Court
        fields = ["surface_type", "is_indoor", "club"]
