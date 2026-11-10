"""Tests for the Celery app wiring (config/celery.py)."""

from contextlib import redirect_stdout
from io import StringIO

from django.test import SimpleTestCase

from config import celery_app
from config.celery import debug_task


class CeleryAppTests(SimpleTestCase):
    def test_app_loads_django_settings_and_discovers_tasks(self):
        celery_app.loader.import_default_modules()

        self.assertEqual(celery_app.main, "court_control")
        self.assertIn("apps.bookings.tasks.send_booking_confirmation_task", celery_app.tasks)

    def test_debug_task_prints_its_request(self):
        out = StringIO()

        with redirect_stdout(out):
            debug_task.apply()  # runs locally, no broker

        self.assertTrue(out.getvalue().startswith("Request: "))
