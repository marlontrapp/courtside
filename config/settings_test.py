"""
Test settings for CourtControl.

Usage:
    python manage.py test --settings=config.settings_test

Inherits everything from the base settings and overrides only what makes
the test suite fast, deterministic, and free of external service
dependencies (Redis broker/cache, email providers, static manifests).

PostgreSQL is still required: the Reservation model relies on a
PostgreSQL ExclusionConstraint (btree_gist), which has no SQLite
equivalent. The test runner creates/destroys its own `test_*` database
from DATABASE_URL.
"""

from .settings import *  # noqa: F401,F403

# --- Speed -----------------------------------------------------------------
# MD5 is intentionally weak; it makes user creation in tests ~100x faster.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# --- External services -----------------------------------------------------
# No Redis needed to run the suite.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Run Celery tasks synchronously in-process; surface task exceptions.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# Capture outgoing mail in django.core.mail.outbox instead of sending.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# --- Static files ----------------------------------------------------------
# The WhiteNoise manifest storage requires `collectstatic` to have run;
# plain storage avoids that requirement when rendering admin pages in tests.
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}
# Serve static files from app dirs without a pre-built STATIC_ROOT.
WHITENOISE_AUTOREFRESH = True

# --- Noise reduction -------------------------------------------------------
LOGGING["root"]["level"] = "CRITICAL"  # noqa: F405
LOGGING["loggers"]["apps"]["level"] = "CRITICAL"  # noqa: F405
LOGGING["loggers"]["celery"]["level"] = "CRITICAL"  # noqa: F405
# Records propagated from child loggers (e.g. django.request 4xx warnings)
# bypass ancestor logger levels, so silence at the handler.
LOGGING["handlers"]["console"]["level"] = "CRITICAL"  # noqa: F405
