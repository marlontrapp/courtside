"""Pagination classes shared by the API."""

from django.conf import settings
from rest_framework.exceptions import NotFound
from rest_framework.pagination import BasePagination
from rest_framework.response import Response
from rest_framework.utils.urls import remove_query_param, replace_query_param


class NoCountPageNumberPagination(BasePagination):
    """
    Page-number pagination without a total count: fetches one row more than
    the page size to know whether there is a next page, instead of running
    a COUNT(*) over every match — for searches, where counting can cost as
    much as the search itself.

    Response: {"next": url | null, "previous": url | null, "results": [...]}.
    A page past the end is empty; a page that isn't a positive integer is a 404.
    """

    page_size = settings.REST_FRAMEWORK["PAGE_SIZE"]
    page_query_param = "page"

    def paginate_queryset(self, queryset, request, view=None):
        self.request = request
        raw = request.query_params.get(self.page_query_param, "1")
        try:
            self.page = int(raw)
        except ValueError:
            self.page = 0
        if self.page < 1:
            raise NotFound("Invalid page.")
        offset = (self.page - 1) * self.page_size
        rows = list(queryset[offset : offset + self.page_size + 1])
        self.has_next = len(rows) > self.page_size
        return rows[: self.page_size]

    def get_paginated_response(self, data):
        return Response({"next": self.get_next_link(), "previous": self.get_previous_link(), "results": data})

    def get_next_link(self):
        if not self.has_next:
            return None
        return replace_query_param(self.request.build_absolute_uri(), self.page_query_param, self.page + 1)

    def get_previous_link(self):
        if self.page == 1:
            return None
        url = self.request.build_absolute_uri()
        if self.page == 2:
            return remove_query_param(url, self.page_query_param)
        return replace_query_param(url, self.page_query_param, self.page - 1)

    def get_schema_operation_parameters(self, view):
        return [{
            "name": self.page_query_param,
            "required": False,
            "in": "query",
            "description": f"Page number ({self.page_size} per page); a page past the end is empty.",
            "schema": {"type": "integer", "minimum": 1},
        }]

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["results"],
            "properties": {
                "next": {"type": "string", "nullable": True, "format": "uri"},
                "previous": {"type": "string", "nullable": True, "format": "uri"},
                "results": schema,
            },
        }
