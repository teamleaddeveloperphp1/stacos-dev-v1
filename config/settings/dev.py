"""Local development settings."""

from .base import *
from .base import BASE_DIR, INSTALLED_APPS, LOGGING, MIDDLEWARE, env

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "[::1]"]

# The Framework7 dev server runs on :3002 and talks to /api/v1 here.
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://localhost:3002"]
CORS_ALLOWED_ORIGINS = ["http://localhost:3002"]

INSTALLED_APPS += ["debug_toolbar", "django_browser_reload"]
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.common.CommonMiddleware"),
    "debug_toolbar.middleware.DebugToolbarMiddleware",
)
# Reload the browser when a template, a stylesheet or the script bundle changes.
#
# Placed immediately after the toolbar so it sees the finished HTML. It appends
# its script tag before ``</body>`` and does nothing to a response that has no
# ``</body>`` — which is every HTMX fragment — so unlike the toolbar it needs no
# opt-out callback. Nothing here loads under production settings.
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.common.CommonMiddleware"),
    "django_browser_reload.middleware.BrowserReloadMiddleware",
)
INTERNAL_IPS = ["127.0.0.1"]
DEBUG_TOOLBAR_CONFIG = {
    # Start collapsed to the handle. Expanded, the toolbar is a fixed 220px
    # panel pinned to the right edge at z-index 100000000 — and every card in
    # this product puts its primary action in the card header, top right, which
    # is exactly what that panel lands on. Clicks then hit the toolbar instead
    # of the button, with no error and no request: the observed symptom was
    # "Create my calendar" doing nothing at all on the setup flow's last step.
    # This only sets the default; the toolbar remembers its own open/closed
    # state per browser in `localStorage["djdt.show"]` afterwards.
    "SHOW_COLLAPSED": True,
    # The toolbar tries to inject itself into HTMX fragment responses, which
    # corrupts them. Only show it on full-page renders.
    "SHOW_TOOLBAR_CALLBACK": lambda request: (
        DEBUG
        and request.META.get("REMOTE_ADDR") in INTERNAL_IPS
        and not request.headers.get("HX-Request")
    ),
}

# ---------------------------------------------------------------------------
# Static files in development
#
# Stated rather than inherited. WhiteNoise derives both of these from
# ``settings.DEBUG`` by default, which is correct today and silently wrong the
# moment somebody runs the dev server with DEBUG off to reproduce something —
# they would then be served whatever `collectstatic` last wrote to
# ``staticfiles/``, which in this repository was a month stale.
#
# ``WHITENOISE_MAX_AGE = 0`` matters as much as the autorefresh: `{% static %}`
# emits an unhashed URL while DEBUG is on (``HashedFilesMixin`` short-circuits),
# so the only thing stopping the browser reusing yesterday's stylesheet is the
# absence of a cache header. The default is 60 seconds, which is long enough for
# a rebuild to look like it did nothing.
# ---------------------------------------------------------------------------
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True
WHITENOISE_MAX_AGE = 0

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
