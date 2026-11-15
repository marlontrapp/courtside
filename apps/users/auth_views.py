"""
Account endpoints (/api/v1/auth/…): sign up, log in with a password or an
emailed code, reset the password, log out — plus GET /api/v1/me/.

Logging in starts a session (cookie), like /api-auth/login/. The POSTs here
check the CSRF token even for anonymous requests (so another site can't log
a visitor into an attacker's account): get the token from GET
/api/v1/auth/csrf/ (or any page setting the csrftoken cookie).

Errors about codes and rate limits are returned, not raised: the project
wraps requests in a transaction that DRF rolls back on raised API errors, and
a wrong code's used-up attempt must be kept.
"""

from django.conf import settings
from django.contrib.auth import login, logout
from django.core.exceptions import ValidationError as DjangoValidationError
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from . import auth, openapi
from .serializers import (
    AccountSerializer,
    CodeSerializer,
    EmailSerializer,
    MeSerializer,
    PasswordLoginSerializer,
    PasswordResetConfirmSerializer,
    SignupSerializer,
)

_BACKEND = "django.contrib.auth.backends.ModelBackend"


class AuthRateThrottle(SimpleRateThrottle):
    """Requests per client IP to the auth endpoints (settings.AUTH_THROTTLE_RATE, read per request)."""

    scope = "auth"

    def get_rate(self):
        return settings.AUTH_THROTTLE_RATE

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AuthView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # DRF's session auth only checks CSRF for logged-in users: check it when
        # anonymous too, so another site can't log a visitor into an attacker's account.
        if not request.user.is_authenticated:
            SessionAuthentication().enforce_csrf(request)

    def valid(self, serializer_class):
        serializer = serializer_class(data=self.request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def start_session(self, user, status_code=status.HTTP_200_OK) -> Response:
        login(self.request, user, backend=_BACKEND)  # new session key; CSRF token rotated
        return Response(AccountSerializer(user).data, status=status_code)

    @staticmethod
    def challenge(request_code) -> Response:
        try:
            challenge = request_code()
        except auth.CodeRateLimited as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS,
                            headers={"Retry-After": str(exc.retry_after)})
        return Response(
            {
                "challenge_id": str(challenge.pk),
                "expires_at": challenge.expires_at.isoformat(),
                "detail": "If the email can be used for this, a code was sent to it.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @staticmethod
    def invalid_code(exc: auth.InvalidCode) -> Response:
        return Response({"code": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)


@method_decorator(ensure_csrf_cookie, name="get")
@openapi.auth_csrf
class CsrfView(AuthView):
    def get(self, request):
        return Response({"csrf_token": get_token(request)})


@openapi.auth_signup
class SignupView(AuthView):
    def post(self, request):
        data = self.valid(SignupSerializer)
        return self.challenge(lambda: auth.request_signup(
            email=data["email"], first_name=data["first_name"], last_name=data["last_name"],
            password=data.get("password"),
        ))


@openapi.auth_signup_verify
class SignupVerifyView(AuthView):
    def post(self, request):
        data = self.valid(CodeSerializer)
        try:
            result = auth.verify_signup(data["challenge_id"], data["code"])
        except auth.InvalidCode as exc:
            return self.invalid_code(exc)
        return self.start_session(result.user, status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)


@openapi.auth_login
class PasswordLoginView(AuthView):
    def post(self, request):
        data = self.valid(PasswordLoginSerializer)
        user = auth.authenticate_password(data["email"], data["password"])
        if user is None:
            return Response({"non_field_errors": ["Invalid email or password."]}, status=status.HTTP_400_BAD_REQUEST)
        return self.start_session(user)


@openapi.auth_login_code
class LoginCodeView(AuthView):
    def post(self, request):
        data = self.valid(EmailSerializer)
        return self.challenge(lambda: auth.request_login_code(data["email"]))


@openapi.auth_login_code_verify
class LoginCodeVerifyView(AuthView):
    def post(self, request):
        data = self.valid(CodeSerializer)
        try:
            user = auth.verify_login_code(data["challenge_id"], data["code"])
        except auth.InvalidCode as exc:
            return self.invalid_code(exc)
        return self.start_session(user)


@openapi.auth_password_reset
class PasswordResetView(AuthView):
    def post(self, request):
        data = self.valid(EmailSerializer)
        return self.challenge(lambda: auth.request_password_reset(data["email"]))


@openapi.auth_password_reset_confirm
class PasswordResetConfirmView(AuthView):
    def post(self, request):
        data = self.valid(PasswordResetConfirmSerializer)
        try:
            user = auth.confirm_password_reset(data["challenge_id"], data["code"], data["new_password"])
        except auth.InvalidCode as exc:
            return self.invalid_code(exc)
        except DjangoValidationError as exc:
            return Response({"new_password": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
        return self.start_session(user)


@openapi.auth_logout
class LogoutView(AuthView):
    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


@openapi.me
class MeView(APIView):
    """
    GET /api/v1/me/ — the signed-in user and how they can act:

        {"id": "<uuid>", "email": "ana@example.com", "first_name": "Ana", "last_name": "Silva",
         "is_player": true,          # has a player profile
         "is_site_staff": false,     # site-wide staff: can work in every club
         "clubs": [                  # clubs they can work in, by name
            {"id": "<uuid>", "name": "Jardins Country Club", "slug": "…", "address": "…",
             "timezone": "America/Sao_Paulo", "access_type": "MEMBERS_ONLY", "is_active": true,
             "roles": ["ADMIN", "FRONT_DESK"]}]}   # staff roles held today

    403 when not signed in.
    """

    def get(self, request):
        return Response(MeSerializer(request.user).data)
