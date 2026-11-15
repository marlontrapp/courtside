"""
Referring to players in API input by email or by player id (the user id —
what API responses show; emails are never returned).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q


@dataclass(frozen=True)
class PlayerReferences:
    emails: list[str]  # lower-cased
    ids: list[uuid.UUID]
    invalid: list[str]  # entries that are neither (repr'd, for messages)

    def __bool__(self) -> bool:
        return bool(self.emails or self.ids)

    def q(self, user_field: str = "") -> Q:
        """Q matching the referenced users; `user_field` is the path to the user ("" = User itself)."""
        prefix = f"{user_field}__" if user_field else ""
        lookup = Q(**{f"{prefix}pk__in": self.ids})
        for email in self.emails:
            lookup |= Q(**{f"{prefix}email__iexact": email})
        return lookup


def parse_player_references(entries: list[str]) -> PlayerReferences:
    """
    Sort entries into emails, player ids and invalid entries, each without
    duplicates. An entry is a player id if it parses as a UUID (emails always
    contain "@"), otherwise it must be a valid email (case-insensitive).
    """
    emails: dict[str, None] = {}
    ids: dict[uuid.UUID, None] = {}
    invalid: dict[str, None] = {}
    for entry in (entry.strip() for entry in entries):
        if not entry:
            continue
        try:
            ids[uuid.UUID(entry)] = None
            continue
        except ValueError:
            pass
        try:
            validate_email(entry)
        except ValidationError:
            invalid[repr(entry)] = None
        else:
            emails[entry.lower()] = None
    return PlayerReferences(list(emails), list(ids), list(invalid))
