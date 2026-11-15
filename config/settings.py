"""Environment-driven Django settings for CourtControl."""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_PLAYER_PICTURE_MAX_SIZE_KB = 2048

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    RESERVATION_HOLD_MINUTES=(int, 15),
    PLAYER_PICTURE_MAX_SIZE_KB=(int, DEFAULT_PLAYER_PICTURE_MAX_SIZE_KB),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third-party
    "rest_framework",
    "django_filters",
    "corsheaders",
    "django_celery_beat",
    "drf_spectacular",
    # Local apps
    "apps.common",
    "apps.users",
    "apps.courts",
    "apps.bookings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://courtcontrol:courtcontrol@localhost:5432/courtcontrol",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)

# CACHE_URL: a Redis URL (default: REDIS_URL), or "locmem://" for an in-process
# cache (`make run` without Redis). Used by the auth endpoints' rate limiting.
_CACHE_URL = env("CACHE_URL", default=env("REDIS_URL", default="redis://localhost:6379/0"))
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    if _CACHE_URL.startswith("locmem")
    else {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": _CACHE_URL}
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTH_USER_MODEL = "users.User"

# Where /api-auth/login/ sends users when no ?next= is given
# (Django's default, /accounts/profile/, does not exist here).
LOGIN_REDIRECT_URL = "courts:court-list"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Uploaded files on local disk (development). Served at MEDIA_URL only with DEBUG.
MEDIA_URL = "/media/"
MEDIA_ROOT = env.path("DJANGO_MEDIA_ROOT", default=BASE_DIR / "media")
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
    # Player profile pictures; PlayerProfile.picture_key is a key in this
    # storage. Local disk (MEDIA_ROOT) by default — to use S3 or another
    # CDN-backed service, point this entry at its backend, e.g. with
    # django-storages: {"BACKEND": "storages.backends.s3.S3Storage",
    # "OPTIONS": {"bucket_name": ..., "custom_domain": ...}}.
    "player_pictures": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# OpenAPI schema (drf-spectacular): `make openapi` writes docs/openapi.yaml.
# The long description lives in config/openapi_description.md.
SPECTACULAR_SETTINGS = {
    "TITLE": "CourtControl API",
    "VERSION": "1.0.0",
    "DESCRIPTION": (BASE_DIR / "config" / "openapi_description.md").read_text(encoding="utf-8"),
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {},
}

CORS_ALLOW_ALL_ORIGINS = DEBUG

# Celery
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Run tasks inline in the calling process instead of sending them to the broker.
# `make run` sets it when Redis isn't installed (no broker, no worker): otherwise
# every task dispatch — e.g. a booking's confirmation — blocks retrying Redis.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "expire-unpaid-reservations-every-5-minutes": {
        "task": "apps.bookings.tasks.expire_unpaid_reservations_task",
        "schedule": timedelta(minutes=5),
    },
    "purge-expired-email-challenges-daily": {
        "task": "apps.users.tasks.purge_email_challenges_task",
        "schedule": timedelta(days=1),
    },
    "send-upcoming-match-reminders-every-5-minutes": {
        "task": "apps.bookings.tasks.send_upcoming_match_reminder_task",
        "schedule": timedelta(minutes=5),
    },
}

# Email. EMAIL_URL (django-environ): the console backend by default — emails,
# e.g. login codes, are printed to the server's output; in production e.g.
# smtp+tls://user:password@smtp.example.com:587
vars().update(env.email_url("EMAIL_URL", default="consolemail://"))
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="CourtControl <no-reply@courtcontrol.local>")

# Sign-up, login and password-reset codes sent by email (apps/users/auth.py).
AUTH_CODE_TTL_MINUTES = env.int("AUTH_CODE_TTL_MINUTES", default=10)
AUTH_CODE_MAX_ATTEMPTS = env.int("AUTH_CODE_MAX_ATTEMPTS", default=5)
AUTH_CODE_RESEND_SECONDS = env.int("AUTH_CODE_RESEND_SECONDS", default=60)  # per email
AUTH_CODE_MAX_PER_HOUR = env.int("AUTH_CODE_MAX_PER_HOUR", default=5)  # per email
# Requests per client IP to the auth endpoints (login, signup, codes, reset).
AUTH_THROTTLE_RATE = env("AUTH_THROTTLE_RATE", default="30/min")

# Domain settings
RESERVATION_HOLD_MINUTES = env("RESERVATION_HOLD_MINUTES")
# Self-service club registration: clubs a (non-staff) user may have awaiting
# approval at once, and who is emailed about new registrations (comma-separated;
# empty = nobody — staff can still check GET /api/v1/clubs/pending/).
CLUB_REGISTRATION_MAX_PENDING = env.int("CLUB_REGISTRATION_MAX_PENDING", default=3)
CLUB_REVIEW_NOTIFY_EMAILS = env.list("CLUB_REVIEW_NOTIFY_EMAILS", default=[])
# Largest accepted profile picture upload, in KB (1 KB = 1024 bytes).
PLAYER_PICTURE_MAX_SIZE_KB = env("PLAYER_PICTURE_MAX_SIZE_KB")
if PLAYER_PICTURE_MAX_SIZE_KB <= 0:
    raise ValueError("PLAYER_PICTURE_MAX_SIZE_KB must be a positive number of KB.")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
