"""Test helpers shared across apps."""

from datetime import datetime
from unittest import mock


def freeze_time(moment: datetime):
    """
    Patch `django.utils.timezone.now` to return `moment` (aware datetime).

    Also affects `timezone.localdate()` / `localtime()` and auto_now fields,
    since they call `timezone.now()` internally. Code that does
    `from django.utils.timezone import now` at import time is NOT affected —
    always use `timezone.now()` in application code.

    Usable as a decorator, a context manager, or via start()/stop().
    """
    return mock.patch("django.utils.timezone.now", return_value=moment)


class FrozenTimeMixin:
    """TestCase mixin: freeze time at `frozen_now` for setUp and every test."""

    frozen_now: datetime

    def setUp(self):
        patcher = freeze_time(self.frozen_now)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
