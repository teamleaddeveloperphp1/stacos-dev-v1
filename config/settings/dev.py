"""Local development settings."""

from .base import *
from .base import BASE_DIR, INSTALLED_APPS, LOGGING, MIDDLEWARE, env

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "[::1]"]

# The Framework7 dev server runs on :3002 and talks to /api/v1 here.
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://localhost:3002"]
CORS_ALLOWED_ORIGINS = ["http://localhost:3002"]

INSTALLED_APPS += ["debug_toolbar"]
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.common.CommonMiddleware"),
    "debug_toolbar.middleware.DebugToolbarMiddleware",
)
INTERNAL_IPS = ["127.0.0.1"]
DEBUG_TOOLBAR_CONFIG = {
    # The toolbar tries to inject itself into HTMX fragment responses, which
    # corrupts them. Only show it on full-page renders.
    "SHOW_TOOLBAR_CALLBACK": lambda request: (
        DEBUG
        and request.META.get("REMOTE_ADDR") in INTERNAL_IPS
        and not request.headers.get("HX-Request")
    ),
}

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)

# Media is served locally in dev; production uses S3 with presigned URLs only.
MEDIA_ROOT = BASE_DIR / "media"

# Show SQL in the console when debugging query counts.
if env.bool("LOG_SQL", default=False):
    LOGGING["loggers"]["django.db.backends"] = {"level": "DEBUG", "propagate": True}

# Passwords in dev are typed often; keep validation but drop the length floor.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
]
