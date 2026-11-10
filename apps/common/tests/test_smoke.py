"""Project-level smoke tests: URL wiring and shared factory sanity."""

from django.test import TestCase
from django.urls import reverse

from apps.common.tests.factories import create_reservation


class HealthcheckTests(TestCase):
    def test_health_endpoint_is_public_and_ok(self):
        response = self.client.get(reverse("healthcheck"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"status": "ok", "service": "courtcontrol"}
        )


class FactorySanityTests(TestCase):
    def test_reservation_factory_builds_full_object_graph(self):
        reservation = create_reservation()

        self.assertIsNotNone(reservation.pk)
        self.assertIsNotNone(reservation.court.club.pk)
        self.assertIsNotNone(reservation.primary_player.pk)
        self.assertGreater(reservation.end_datetime, reservation.start_datetime)
