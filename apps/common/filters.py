"""Reusable django-filter building blocks."""

from django_filters import rest_framework as filters


class ChoiceInFilter(filters.BaseInFilter, filters.ChoiceFilter):
    """Comma-separated list of choice values, e.g. ?surface_type=CLAY,HARD."""


class UUIDInFilter(filters.BaseInFilter, filters.UUIDFilter):
    """Comma-separated list of UUIDs."""


class CharInFilter(filters.BaseInFilter, filters.CharFilter):
    """Comma-separated list of strings."""
