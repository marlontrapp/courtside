"""URL routes for the player profile API (mounted under /api/v1/)."""

from django.urls import path

from . import auth_views
from .views import MyProfilePictureView, PlayerSearchView

app_name = "users"

urlpatterns = [
    path("auth/csrf/", auth_views.CsrfView.as_view(), name="auth-csrf"),
    path("auth/signup/", auth_views.SignupView.as_view(), name="auth-signup"),
    path("auth/signup/verify/", auth_views.SignupVerifyView.as_view(), name="auth-signup-verify"),
    path("auth/login/", auth_views.PasswordLoginView.as_view(), name="auth-login"),
    path("auth/login/code/", auth_views.LoginCodeView.as_view(), name="auth-login-code"),
    path("auth/login/code/verify/", auth_views.LoginCodeVerifyView.as_view(), name="auth-login-code-verify"),
    path("auth/password/reset/", auth_views.PasswordResetView.as_view(), name="auth-password-reset"),
    path("auth/password/reset/confirm/", auth_views.PasswordResetConfirmView.as_view(),
         name="auth-password-reset-confirm"),
    path("auth/logout/", auth_views.LogoutView.as_view(), name="auth-logout"),
    path("me/", auth_views.MeView.as_view(), name="me"),
    path("me/profile/picture/", MyProfilePictureView.as_view(), name="my-profile-picture"),
    path("players/search/", PlayerSearchView.as_view(), name="player-search"),
]
