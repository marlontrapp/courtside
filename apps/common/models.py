"""Shared abstract models used across CourtControl apps."""

import uuid

from django.db import models


class TimeStampedUUIDModel(models.Model):
    """Base model with UUID primary key and audit timestamps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
