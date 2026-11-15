"""Serializers for the player profile API."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from django.core.exceptions import ValidationError as DjangoValidationError

from apps.courts.models import Club
from apps.courts.services import get_working_clubs

from .auth import validate_signup_password
from .models import PlayerProfile, User
from .services import picture_url


class PictureUploadSerializer(serializers.Serializer):
    """Input for PUT /api/v1/me/profile/picture/ (multipart). Format and size are checked by the service."""

    picture = serializers.FileField(allow_empty_file=False)


class PlayerSummarySerializer(serializers.ModelSerializer):
    """
    How the API shows a player anywhere outside their own profile: id and
    name only. Emails are never returned (only a player's own profile may show
    theirs); the id is what API input accepts to refer to a player.
    """

    class Meta:
        model = User
        fields = ["id", "first_name", "last_name"]
        read_only_fields = fields


def sorted_by_name(users) -> list[User]:
    """Players in display order (name, then id) — never by email."""
    return sorted(users, key=lambda user: (user.first_name.casefold(), user.last_name.casefold(), str(user.pk)))


class PlayerSearchResultSerializer(serializers.ModelSerializer):
    """
    A player in search results (GET /api/v1/players/search/): only what other
    players may see — no email, phone, birth date or gender.
    """

    id = serializers.UUIDField(source="user_id", read_only=True, help_text="The player's user id.")
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    picture_url = serializers.SerializerMethodField()

    class Meta:
        model = PlayerProfile
        # Must only read fields loaded by apps.users.search.RESULT_FIELDS
        # (anything else costs one extra query per result).
        fields = ["id", "first_name", "last_name", "location", "skill_level", "picture_url"]
        read_only_fields = fields

    @extend_schema_field({"type": "string", "format": "uri", "nullable": True})
    def get_picture_url(self, profile: PlayerProfile) -> str | None:
        return picture_url(profile.picture_key, self.context.get("request"))


# --- Accounts (apps/users/auth.py) -----------------------------------------------------


class AccountSerializer(serializers.ModelSerializer):
    """The logged-in user's own account — the only place a user's email is returned."""

    has_password = serializers.SerializerMethodField(
        help_text="False for an account that logs in with codes only (it can set one via the password reset)."
    )

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "has_password"]
        read_only_fields = fields

    def get_has_password(self, user) -> bool:
        return user.has_usable_password()


class WorkingClubSerializer(serializers.ModelSerializer):
    """A club the user can work in, with their current staff roles there."""

    roles = serializers.SerializerMethodField(
        help_text="ADMIN | GROUNDSKEEPER | FRONT_DESK held today; empty for site staff without a club role."
    )

    class Meta:
        model = Club
        fields = ["id", "name", "slug", "address", "timezone", "access_type", "is_active", "roles"]

    def get_roles(self, club) -> list[str]:
        return self.context["roles"][club.pk]


class MeSerializer(serializers.ModelSerializer):
    """
    The signed-in user: who they are and how they can act — as a player (has a
    player profile), and/or in the clubs they help run (`clubs`, by name).
    """

    is_player = serializers.SerializerMethodField()
    is_site_staff = serializers.BooleanField(source="is_staff", read_only=True)
    clubs = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "is_player", "is_site_staff", "clubs"]

    def get_is_player(self, user) -> bool:
        return PlayerProfile.objects.filter(user=user).exists()

    def get_clubs(self, user) -> list[dict]:
        working = get_working_clubs(user)
        roles = {club.pk: club_roles for club, club_roles in working}
        return WorkingClubSerializer(
            [club for club, _ in working], many=True, context={"roles": roles}
        ).data


class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    password = serializers.CharField(
        required=False, write_only=True, trim_whitespace=False,
        help_text="Optional: without one, log in with codes sent by email.",
    )

    def validate(self, attrs):
        if attrs.get("password"):
            try:
                validate_signup_password(
                    attrs["password"], email=attrs["email"], first_name=attrs["first_name"], last_name=attrs["last_name"]
                )
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class ChallengeSerializer(serializers.Serializer):
    """A code was emailed (if the email is registered — or, for signups, in any case)."""

    challenge_id = serializers.UUIDField(help_text="Send it back with the code.")
    expires_at = serializers.DateTimeField()
    detail = serializers.CharField()


class CodeSerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.CharField(max_length=12, help_text="The 6-digit code from the email.")


class PasswordResetConfirmSerializer(CodeSerializer):
    new_password = serializers.CharField(trim_whitespace=False, write_only=True)
