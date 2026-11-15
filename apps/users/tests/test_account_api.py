"""Account endpoints: sign up, password / code login, password reset, logout, me (apps/users/auth.py)."""

import re
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.common.tests.factories import create_user
from apps.common.tests.utils import freeze_time
from apps.users.models import EmailChallenge, PlayerProfile, User
from apps.users.tasks import purge_email_challenges_task

STRONG = "a-Strong-pass-2030"
NOW = datetime(2030, 1, 7, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


def url(name: str) -> str:
    return reverse(f"users:{name}")


class AccountAPITestCase(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()  # the per-IP throttle
        self.clock = NOW
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self, **delta):
        self.clock += timedelta(**delta)

    def post(self, name, data, client=None):
        with self.captureOnCommitCallbacks(execute=True):  # emails are sent after commit
            return (client or self.client).post(url(name), data, format="json")

    def last_code(self, to: str) -> str:
        message = [m for m in mail.outbox if m.to == [to]][-1]
        return re.search(r"\b(\d{6})\b", message.body).group(1)

    def me(self, client=None):
        return (client or self.client).get(url("me"))

    def sign_up(self, email="new@example.com", password=None, **names):
        body = {"email": email, "first_name": names.get("first_name", "Nina"),
                "last_name": names.get("last_name", "Nova")}
        if password:
            body["password"] = password
        response = self.post("auth-signup", body)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        return response.data["challenge_id"]


class SignupTests(AccountAPITestCase):
    def test_without_a_password(self):
        challenge = self.sign_up("new@example.com")

        self.assertFalse(User.objects.filter(email="new@example.com").exists(), "created only once verified")
        self.assertEqual(mail.outbox[-1].subject, "Your CourtControl sign-up code")
        response = self.post("auth-signup-verify", {"challenge_id": challenge, "code": self.last_code("new@example.com")})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(email="new@example.com")
        self.assertEqual(response.data, {"id": str(user.pk), "email": "new@example.com", "first_name": "Nina",
                                         "last_name": "Nova", "has_password": False})
        self.assertFalse(user.has_usable_password())
        self.assertTrue(PlayerProfile.objects.filter(user=user).exists())
        self.assertEqual(self.me().data["email"], "new@example.com")  # logged in

    def test_with_a_password(self):
        challenge = self.sign_up("new@example.com", password=STRONG)
        self.post("auth-signup-verify", {"challenge_id": challenge, "code": self.last_code("new@example.com")})

        self.client.logout()
        response = self.post("auth-login", {"email": "new@example.com", "password": STRONG})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["has_password"])

    def test_email_is_stored_lower_case(self):
        challenge = self.sign_up("Nina.Nova@Example.COM")
        self.post("auth-signup-verify", {"challenge_id": challenge, "code": self.last_code("nina.nova@example.com")})

        self.assertTrue(User.objects.filter(email="nina.nova@example.com").exists())

    def test_weak_or_missing_data(self):
        for body, field in (
            ({"email": "x@example.com", "first_name": "A", "last_name": "B", "password": "123"}, "password"),
            ({"email": "ninanova2030@example.com", "first_name": "A", "last_name": "B", "password": "ninanova2030"},
             "password"),  # like the email
            ({"email": "not-an-email", "first_name": "A", "last_name": "B"}, "email"),
            ({"email": "x@example.com", "last_name": "B"}, "first_name"),
        ):
            with self.subTest(body):
                response = self.post("auth-signup", body)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)
        self.assertEqual((len(mail.outbox), EmailChallenge.objects.count()), (0, 0))

    def test_an_existing_account_isnt_revealed_and_its_owner_is_logged_in(self):
        owner = create_user(email="ana@example.com", password=STRONG, first_name="Ana")
        fresh = self.post("auth-signup", {"email": "fresh@example.com", "first_name": "F", "last_name": "R"})

        taken = self.post("auth-signup", {"email": "ANA@example.com", "first_name": "Mallory", "last_name": "X",
                                          "password": "another-Pass-999"})

        # The same answer as for a new email.
        self.assertEqual((taken.status_code, set(taken.data), taken.data["detail"]),
                         (fresh.status_code, set(fresh.data), fresh.data["detail"]))
        self.assertEqual(mail.outbox[-1].subject, "Your CourtControl login code")
        self.assertIn("already have an account", mail.outbox[-1].body)

        verified = self.post("auth-signup-verify", {"challenge_id": taken.data["challenge_id"],
                                                    "code": self.last_code("ana@example.com")})

        self.assertEqual(verified.status_code, status.HTTP_200_OK)  # logged in, nothing created
        owner.refresh_from_db()
        self.assertEqual((owner.first_name, owner.check_password(STRONG)), ("Ana", True))  # untouched
        self.assertEqual(User.objects.filter(email__iexact="ana@example.com").count(), 1)

    def test_someone_elses_signup_for_my_email_cant_set_my_password(self):
        attacker = self.sign_up("victim@example.com", password="attacker-Pass-111")
        self.wait(minutes=2)
        victim = self.sign_up("victim@example.com", password="victim-Pass-222")
        victim_code = self.last_code("victim@example.com")

        # The victim's code doesn't work with the attacker's challenge…
        wrong = self.post("auth-signup-verify", {"challenge_id": attacker, "code": victim_code})
        self.assertEqual(wrong.status_code, status.HTTP_400_BAD_REQUEST)
        # …and the victim's own signup sets the victim's password.
        self.post("auth-signup-verify", {"challenge_id": victim, "code": victim_code})
        user = User.objects.get(email="victim@example.com")
        self.assertTrue(user.check_password("victim-Pass-222"))
        self.assertFalse(user.check_password("attacker-Pass-111"))

    def test_inactive_account(self):
        create_user(email="gone@example.com", is_active=False)

        challenge = self.sign_up("gone@example.com")

        self.assertEqual(mail.outbox, [])
        response = self.post("auth-signup-verify", {"challenge_id": challenge, "code": "000000"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CodeRulesTests(AccountAPITestCase):
    def verify(self, challenge, code):
        return self.post("auth-signup-verify", {"challenge_id": challenge, "code": code})

    def test_single_use(self):
        challenge = self.sign_up()
        code = self.last_code("new@example.com")
        self.assertEqual(self.verify(challenge, code).status_code, status.HTTP_201_CREATED)

        self.assertEqual(self.verify(challenge, code).data, {"code": ["Invalid or expired code. Request a new one."]})

    def test_expires(self):
        challenge = self.sign_up()
        self.wait(minutes=10, seconds=1)

        self.assertEqual(self.verify(challenge, self.last_code("new@example.com")).status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_wrong_codes_use_up_the_attempts(self):
        challenge = self.sign_up()
        code = self.last_code("new@example.com")
        wrong = f"{(int(code) + 1) % 10**6:06d}"
        for _ in range(5):
            self.assertEqual(self.verify(challenge, wrong).status_code, status.HTTP_400_BAD_REQUEST)

        response = self.verify(challenge, code)  # right, but too late

        self.assertEqual(response.data, {"code": ["Too many wrong codes. Request a new one."]})
        self.assertEqual(EmailChallenge.objects.get(pk=challenge).attempts, 5)  # kept despite the errors

    def test_a_new_code_cancels_the_previous_one(self):
        first = self.sign_up()
        first_code = self.last_code("new@example.com")
        self.wait(minutes=2)
        second = self.sign_up()

        self.assertEqual(self.verify(first, first_code).status_code, status.HTTP_400_BAD_REQUEST)  # before using the new
        self.assertEqual(self.verify(second, self.last_code("new@example.com")).status_code, status.HTTP_201_CREATED)

    def test_codes_are_hashed_per_challenge(self):
        from apps.users.auth import _code_hash

        self.assertNotEqual(_code_hash("challenge-a", "123456"), _code_hash("challenge-b", "123456"))

    def test_only_a_hash_is_stored(self):
        challenge = self.sign_up()

        stored = EmailChallenge.objects.get(pk=challenge)
        self.assertNotIn(self.last_code("new@example.com"), stored.code_hash)
        self.assertEqual(len(stored.code_hash), 64)


class PasswordLoginTests(AccountAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = create_user(email="ana@example.com", password=STRONG)

    def test_logs_in_with_any_email_case(self):
        response = self.post("auth-login", {"email": "ANA@Example.com", "password": STRONG})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["id"], str(self.user.pk))
        self.assertEqual(self.me().data["email"], "ana@example.com")

    def test_an_unknown_email_costs_a_password_hash_too(self):
        from apps.users.auth import authenticate_password

        with mock.patch.object(User, "set_password") as hashing:
            self.assertIsNone(authenticate_password("nobody@example.com", "x"))
        hashing.assert_called_once_with("x")  # so response time doesn't tell known emails apart

    def test_failures_say_the_same(self):
        create_user(email="off@example.com", password=STRONG, is_active=False)
        no_password = create_user(email="codes@example.com")
        no_password.set_unusable_password()
        no_password.save()
        for email, password in (("ana@example.com", "wrong"), ("nobody@example.com", STRONG),
                                ("off@example.com", STRONG), ("codes@example.com", "")):
            with self.subTest(email):
                response = self.post("auth-login", {"email": email, "password": password or "x"})

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data, {"non_field_errors": ["Invalid email or password."]})
        self.assertEqual(self.me().status_code, status.HTTP_403_FORBIDDEN)


class LoginCodeTests(AccountAPITestCase):
    def test_log_in_with_a_code(self):
        user = create_user(email="ana@example.com")

        challenge = self.post("auth-login-code", {"email": "ana@example.com"}).data["challenge_id"]
        response = self.post("auth-login-code-verify",
                             {"challenge_id": challenge, "code": self.last_code("ana@example.com")})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["id"], str(user.pk))
        self.assertEqual(mail.outbox[-1].subject, "Your CourtControl login code")

    def test_an_unknown_email_gets_the_same_answer_and_no_email(self):
        create_user(email="ana@example.com")
        known = self.post("auth-login-code", {"email": "ana@example.com"})
        unknown = self.post("auth-login-code", {"email": "nobody@example.com"})

        self.assertEqual((known.status_code, set(known.data), known.data["detail"]),
                         (unknown.status_code, set(unknown.data), unknown.data["detail"]))
        self.assertEqual([m.to for m in mail.outbox], [["ana@example.com"]])
        response = self.post("auth-login-code-verify", {"challenge_id": unknown.data["challenge_id"], "code": "123456"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_login_code_doesnt_verify_a_signup_or_reset(self):
        create_user(email="ana@example.com")
        challenge = self.post("auth-login-code", {"email": "ana@example.com"}).data["challenge_id"]
        code = self.last_code("ana@example.com")

        for name, extra in (("auth-signup-verify", {}), ("auth-password-reset-confirm", {"new_password": STRONG})):
            with self.subTest(name):
                response = self.post(name, {"challenge_id": challenge, "code": code, **extra})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PasswordResetTests(AccountAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = create_user(email="ana@example.com", password="old-Pass-123x")

    def reset(self, new_password=STRONG, email="ana@example.com"):
        challenge = self.post("auth-password-reset", {"email": email}).data["challenge_id"]
        return challenge, self.post("auth-password-reset-confirm", {
            "challenge_id": challenge, "code": self.last_code(email), "new_password": new_password})

    def test_sets_a_new_password_and_logs_out_other_sessions(self):
        other_device = APIClient()
        other_device.post(url("auth-login"), {"email": "ana@example.com", "password": "old-Pass-123x"}, format="json")
        self.assertEqual(self.me(other_device).status_code, status.HTTP_200_OK)

        _challenge, response = self.reset()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.me().status_code, status.HTTP_200_OK)  # this client: logged in
        self.assertEqual(self.me(other_device).status_code, status.HTTP_403_FORBIDDEN)  # the other: logged out
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(STRONG))
        self.assertEqual(mail.outbox[-1].subject, "Reset your CourtControl password")

    def test_a_rejected_password_doesnt_use_up_the_code(self):
        challenge, response = self.reset(new_password="123")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.data)
        retry = self.post("auth-password-reset-confirm", {
            "challenge_id": challenge, "code": self.last_code("ana@example.com"), "new_password": STRONG})
        self.assertEqual(retry.status_code, status.HTTP_200_OK, retry.data)

    def test_sets_a_first_password_for_a_code_only_account(self):
        self.user.set_unusable_password()
        self.user.save()

        _challenge, response = self.reset()

        self.assertTrue(response.data["has_password"])

    def test_unknown_email(self):
        response = self.post("auth-password-reset", {"email": "nobody@example.com"})

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(mail.outbox, [])


class RateLimitTests(AccountAPITestCase):
    def request(self, email="ana@example.com"):
        return self.post("auth-login-code", {"email": email})

    def test_one_code_a_minute_per_email(self):
        self.assertEqual(self.request().status_code, status.HTTP_202_ACCEPTED)
        self.wait(seconds=20)

        limited = self.request()

        self.assertEqual(limited.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(limited["Retry-After"], "40")
        self.assertEqual(self.request("other@example.com").status_code, status.HTTP_202_ACCEPTED)
        self.wait(seconds=40)
        self.assertEqual(self.request().status_code, status.HTTP_202_ACCEPTED)

    def test_the_minute_is_per_purpose(self):
        self.assertEqual(self.request().status_code, status.HTTP_202_ACCEPTED)

        reset = self.post("auth-password-reset", {"email": "ana@example.com"})

        self.assertEqual(reset.status_code, status.HTTP_202_ACCEPTED)

    def test_the_hour_counts_every_purpose(self):
        for name in ("auth-login-code", "auth-password-reset", "auth-login-code", "auth-password-reset",
                     "auth-login-code"):
            self.assertEqual(self.post(name, {"email": "ana@example.com"}).status_code, status.HTTP_202_ACCEPTED)
            self.wait(minutes=2)

        self.assertEqual(self.post("auth-password-reset", {"email": "ana@example.com"}).status_code,
                         status.HTTP_429_TOO_MANY_REQUESTS)

    def test_five_an_hour_per_email_known_or_not(self):
        create_user(email="ana@example.com")
        for email in ("ana@example.com", "nobody@example.com"):
            with self.subTest(email):
                for _ in range(5):
                    self.assertEqual(self.request(email).status_code, status.HTTP_202_ACCEPTED)
                    self.wait(minutes=2)
                self.assertEqual(self.request(email).status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_settings(AUTH_THROTTLE_RATE="3/min")
    def test_requests_per_client(self):
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            self.assertNotEqual(self.post("auth-login", {"email": email, "password": "x"}).status_code,
                                status.HTTP_429_TOO_MANY_REQUESTS)

        self.assertEqual(self.post("auth-login", {"email": "d@example.com", "password": "x"}).status_code,
                         status.HTTP_429_TOO_MANY_REQUESTS)


class CsrfTests(AccountAPITestCase):
    def test_anonymous_posts_need_the_csrf_token(self):
        create_user(email="ana@example.com", password=STRONG)
        browser = APIClient(enforce_csrf_checks=True)
        body = {"email": "ana@example.com", "password": STRONG}

        self.assertEqual(browser.post(url("auth-login"), body, format="json").status_code, status.HTTP_403_FORBIDDEN)

        token = browser.get(url("auth-csrf")).data["csrf_token"]
        self.assertIn("csrftoken", browser.cookies)
        response = browser.post(url("auth-login"), body, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class LogoutAndMeTests(AccountAPITestCase):
    def test_logout(self):
        create_user(email="ana@example.com", password=STRONG)
        self.post("auth-login", {"email": "ana@example.com", "password": STRONG})

        self.assertEqual(self.post("auth-logout", {}).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.me().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.post("auth-logout", {}).status_code, status.HTTP_204_NO_CONTENT)  # again: fine

    def test_me_needs_a_login(self):
        self.assertEqual(self.me().status_code, status.HTTP_403_FORBIDDEN)


class EmailAndCleanupTests(AccountAPITestCase):
    def test_code_email(self):
        create_user(email="ana@example.com")

        self.post("auth-login-code", {"email": "ana@example.com"})

        message = mail.outbox[-1]
        self.assertEqual(message.to, ["ana@example.com"])
        self.assertRegex(message.body, r"\b\d{6}\b")
        self.assertIn("expires in 10 minutes", message.body)

    def test_purge_deletes_challenges_expired_over_a_day_ago(self):
        self.post("auth-login-code", {"email": "old@example.com"})
        self.wait(days=1, minutes=11)
        self.post("auth-login-code", {"email": "recent@example.com"})

        with freeze_time(self.clock):
            self.assertEqual(purge_email_challenges_task(), 1)
        self.assertEqual(list(EmailChallenge.objects.values_list("email", flat=True)), ["recent@example.com"])
        self.assertIsNotNone(timezone.now())
