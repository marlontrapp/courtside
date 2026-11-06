"""CourtControl Django project package.

Expose the Celery application so that `celery -A config` discovers it
and so that shared_task decorators bind to this app instance.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
