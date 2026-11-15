"""OpenAPI documentation of the users app's views (drf-spectacular decorators)."""

from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers

from apps.common.openapi import (
    INVALID_PAGE,
    VALIDATION_ERROR,
    NOT_LOGGED_IN,
    SEARCH_BAD_QUERY,
    SEARCH_RANKING,
    DetailSerializer,
    search_query,
)

from .serializers import (
    AccountSerializer,
    ChallengeSerializer,
    CodeSerializer,
    EmailSerializer,
    PasswordLoginSerializer,
    PasswordResetConfirmSerializer,
    PictureUploadSerializer,
    PlayerSearchResultSerializer,
    SignupSerializer,
)
from .services import PICTURE_FORMAT_NAMES, PICTURE_MAX_PIXELS

PROFILE, PLAYERS, AUTH = ["Profile"], ["Players"], ["Accounts"]

PICTURE_ERROR = inline_serializer("PictureError", fields={"picture": serializers.ListField(child=serializers.CharField())})

my_profile_picture = extend_schema_view(
    put=extend_schema(
        tags=PROFILE,
        summary="Set my profile picture",
        description=f"""
Uploads the logged-in player's profile picture (`multipart/form-data`, one
file field, `picture`) and replaces the previous one; the player profile is
created if the user has none yet.

- **Formats:** still **{PICTURE_FORMAT_NAMES}**, detected from the file's
  content (the filename and content type sent are ignored). Animated
  pictures (animated PNG or WebP) and GIF are rejected. Phone photos saved
  as a JPEG with extra images appended (MPO: HDR gain map, depth map, …) are
  accepted; only the photo is kept. Corrupt or truncated files, and images
  above {PICTURE_MAX_PIXELS // 1_000_000} megapixels, are rejected.
- **Size:** at most the server's `PLAYER_PICTURE_MAX_SIZE_KB` (default
  {settings.DEFAULT_PLAYER_PICTURE_MAX_SIZE_KB} KB); a clearly larger request is refused
  before its body is read. Downscale camera photos first (512–1024 px is
  plenty for an avatar).
- **Metadata is removed:** the picture is re-encoded (same format) without
  EXIF — camera, date, **GPS location** —, XMP, IPTC, comments and PNG text;
  the orientation is applied first, so photos stay upright. The colour
  profile and transparency are kept.
- Each upload gets a new `picture_key` (so CDN caches never serve an old
  picture); `picture_url` is where to fetch it. The previous picture is
  deleted.
""",
        request={"multipart/form-data": PictureUploadSerializer},
        responses={
            200: inline_serializer("PictureSaved", fields={
                "picture_key": serializers.CharField(help_text="Key in the file storage."),
                "picture_url": serializers.URLField(),
            }),
            400: OpenApiResponse(PICTURE_ERROR, description=(
                "No file / empty file, not an image, unsupported format, animated, corrupt, or too many pixels.")),
            403: NOT_LOGGED_IN,
            413: OpenApiResponse(PICTURE_ERROR, description="Larger than the size limit."),
            415: OpenApiResponse(DetailSerializer, description="The body isn't multipart/form-data."),
        },
    ),
    delete=extend_schema(
        tags=PROFILE,
        summary="Remove my profile picture",
        description=(
            "The picture is removed from the profile and deleted from storage; the rest of the profile is "
            "untouched. `204` also when there's no picture or no profile — safe to retry."
        ),
        responses={204: OpenApiResponse(description="Removed (or there was none)."), 403: NOT_LOGGED_IN},
    ),
)

player_search = extend_schema_view(get=extend_schema(
    tags=PLAYERS,
    summary="Find players by name and location",
    description=f"""
Players — **active** users with a player profile — whose name (first / last
name) or **location** (the profile's free-text location: neighbourhood,
city, state, country) contains every word of `q`.

Results show **only public details** — email, phone, birth date and gender
are neither returned nor searchable. `id` is the player's user id: pass it
in a booking's `guests` to add the player without knowing their email.
`picture_url` is `null` without a picture.

{SEARCH_RANKING}
""",
    parameters=[search_query("player's name or location", "`joao` / `JOÃO`, `ana pinheiros`, `lisboa`")],
    responses={200: PlayerSearchResultSerializer(many=True), 400: SEARCH_BAD_QUERY, 403: NOT_LOGGED_IN,
               404: INVALID_PAGE},
))



# --- Accounts -------------------------------------------------------------------------

CSRF_FAILED = OpenApiResponse(DetailSerializer, description="No valid CSRF token (see *Get a CSRF token*).")
RATE_LIMITED = OpenApiResponse(DetailSerializer, description=(
    "Too many requests: too many codes for this email (`Retry-After` header: seconds to wait), or too many "
    "requests from this client."))
BAD_CODE = OpenApiResponse(
    inline_serializer("CodeError", fields={"code": serializers.ListField(child=serializers.CharField())}),
    description="Wrong, expired or already used code, or too many wrong tries (5): request a new one.",
)
CODE_NOTE = (
    "The code has 6 digits, expires after 10 minutes (`expires_at`), allows 5 tries and works once; requesting "
    "a new one cancels the previous. Codes per email: one a minute for each purpose (sign-up, login, password reset), five an hour in all (`429`)."
)
SESSION_NOTE = "Starts a session (cookie) and returns the account; the CSRF token changes (read the new cookie)."

auth_csrf = extend_schema_view(get=extend_schema(
    tags=AUTH, summary="Get a CSRF token",
    description=(
        "Sets the `csrftoken` cookie and returns the token: send it in the `X-CSRFToken` header of every "
        "`POST`/`PUT`/`PATCH`/`DELETE` — the account endpoints check it even before you log in."
    ),
    auth=[],
    responses={200: inline_serializer("CsrfToken", fields={"csrf_token": serializers.CharField()})},
))

auth_signup = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Sign up",
    description=f"""
Emails a code to finish signing up (*Verify sign-up*); the account is only
created then — with this request's name and password. The password is
optional: without one, log in with codes (and set one later through the
password reset). It must pass the password rules (length, not too common,
not all digits, not like your name or email).

The answer is the same whether the email is already registered (to not
reveal it): if it is, its owner gets a code that logs them in instead.
{CODE_NOTE}
""",
    auth=[],
    request=SignupSerializer,
    responses={202: ChallengeSerializer, 400: VALIDATION_ERROR, 403: CSRF_FAILED, 429: RATE_LIMITED},
))

auth_signup_verify = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Verify sign-up (and log in)",
    description=(
        "The `challenge_id` from *Sign up* and the emailed code. Creates the account (`201`), or — the email was "
        f"already registered — logs its owner in (`200`). {SESSION_NOTE}"
    ),
    auth=[],
    request=CodeSerializer,
    responses={
        200: OpenApiResponse(AccountSerializer, description="The email already had an account: logged in."),
        201: OpenApiResponse(AccountSerializer, description="Account created: logged in."),
        400: BAD_CODE, 403: CSRF_FAILED, 429: RATE_LIMITED,
    },
))

auth_login = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Log in with a password",
    description=f"Email (any case) and password. {SESSION_NOTE} Accounts without a password: log in with a code.",
    auth=[],
    request=PasswordLoginSerializer,
    responses={
        200: AccountSerializer,
        400: OpenApiResponse(VALIDATION_ERROR.response, description="Invalid email or password (not saying which)."),
        403: CSRF_FAILED, 429: RATE_LIMITED,
    },
))

auth_login_code = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Request a login code",
    description=(
        "Emails a code to log in without a password (*Log in with a code*). The answer is the same whether "
        f"the email has an account or not. {CODE_NOTE}"
    ),
    auth=[],
    request=EmailSerializer,
    responses={202: ChallengeSerializer, 400: VALIDATION_ERROR, 403: CSRF_FAILED, 429: RATE_LIMITED},
))

auth_login_code_verify = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Log in with a code",
    description=f"The `challenge_id` from *Request a login code* and the emailed code. {SESSION_NOTE}",
    auth=[],
    request=CodeSerializer,
    responses={200: AccountSerializer, 400: BAD_CODE, 403: CSRF_FAILED, 429: RATE_LIMITED},
))

auth_password_reset = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Forgot password: request a code",
    description=(
        "Emails a code to set a new password (*Set a new password*) — also for an account that has none yet. "
        f"The answer is the same whether the email has an account or not. {CODE_NOTE}"
    ),
    auth=[],
    request=EmailSerializer,
    responses={202: ChallengeSerializer, 400: VALIDATION_ERROR, 403: CSRF_FAILED, 429: RATE_LIMITED},
))

auth_password_reset_confirm = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Set a new password",
    description=(
        "The `challenge_id` from *Forgot password*, the emailed code and the new password (password rules "
        "apply; a rejected password doesn't use up the code). **Every other session of the account is logged "
        f"out.** {SESSION_NOTE}"
    ),
    auth=[],
    request=PasswordResetConfirmSerializer,
    responses={
        200: AccountSerializer,
        400: OpenApiResponse(VALIDATION_ERROR.response, description=(
            "Bad code (`code`), or the password breaks the rules (`new_password`).")),
        403: CSRF_FAILED, 429: RATE_LIMITED,
    },
))

auth_logout = extend_schema_view(post=extend_schema(
    tags=AUTH, summary="Log out",
    description="Ends the session. `204` also when not logged in.",
    request=None,
    responses={204: OpenApiResponse(description="Logged out."), 403: CSRF_FAILED},
))

me = extend_schema_view(get=extend_schema(
    tags=PROFILE, summary="My account",
    description="The logged-in user's account — the only endpoint returning a user's email (their own).",
    responses={200: AccountSerializer, 403: NOT_LOGGED_IN},
))
