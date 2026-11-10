"""Session login/logout through DRF's /api-auth/ views."""

from django.test import Client, TestCase
from django.urls import reverse

from apps.common.tests.factories import DEFAULT_PASSWORD, create_user

LOGIN_URL = reverse("rest_framework:login")
LOGOUT_URL = reverse("rest_framework:logout")
COURTS_URL = reverse("courts:court-list")


class ApiSessionAuthTests(TestCase):
    def setUp(self):
        # Enforce CSRF like a real browser/curl session would.
        self.client = Client(enforce_csrf_checks=True)
        self.player = create_user(email="player@example.com")  # regular, non-staff

    def _csrf_token(self) -> str:
        response = self.client.get(LOGIN_URL)
        self.assertEqual(response.status_code, 200)
        return response.cookies["csrftoken"].value

    def _login(self, password: str = DEFAULT_PASSWORD, **extra):
        return self.client.post(
            LOGIN_URL,
            {
                "csrfmiddlewaretoken": self._csrf_token(),
                "username": "player@example.com",  # AuthenticationForm field; holds the email
                "password": password,
                **extra,
            },
        )

    def test_login_page_renders(self):
        response = self.client.get(LOGIN_URL)

        self.assertContains(response, 'name="username"')

    def test_non_staff_user_can_log_in_and_call_the_api(self):
        self.assertFalse(self.player.is_staff)
        self.assertEqual(self.client.get(COURTS_URL).status_code, 403)

        response = self._login()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(COURTS_URL).status_code, 200)

    def test_login_redirects_to_courts_by_default(self):
        response = self._login()

        self.assertRedirects(response, COURTS_URL, fetch_redirect_response=False)

    def test_login_honours_next(self):
        response = self._login(next="/health/")

        self.assertRedirects(response, "/health/", fetch_redirect_response=False)

    def test_wrong_password_is_rejected(self):
        response = self._login(password="wrong-password")

        self.assertEqual(response.status_code, 200)  # form re-rendered with errors
        self.assertNotIn("sessionid", self.client.cookies)
        self.assertEqual(self.client.get(COURTS_URL).status_code, 403)

    def test_login_without_csrf_token_is_rejected(self):
        response = self.client.post(
            LOGIN_URL, {"username": "player@example.com", "password": DEFAULT_PASSWORD}
        )

        self.assertEqual(response.status_code, 403)

    def test_logout_ends_the_session(self):
        self._login()

        response = self.client.post(
            LOGOUT_URL, {"csrfmiddlewaretoken": self.client.cookies["csrftoken"].value}
        )

        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(self.client.get(COURTS_URL).status_code, 403)
