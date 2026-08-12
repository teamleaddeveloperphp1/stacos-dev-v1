"""
Test settings.

Two deliberate choices:

  * Row-Level Security stays ENABLED. The isolation suite is the specification of
    the tenancy model, and running it without RLS would test half the design.
  * Celery is eager only where a test does not care about the broker. Eager mode
    runs tasks in-process with the caller's contextvars already bound, so
    TenantTask's binding logic is never exercised — tests that assert task
    scoping must use a real broker or explicitly clear context first.
"""

from .base import *
from .base import MIDDLEWARE, env

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

SECRET_KEY = "test-only-key-not-secret"  # noqa: S105

# Migrations run as the owner role; tests create test_stacos from that connection.
DATABASES = {
    "default": env.db_url(
        "DATABASE_MIGRATE_URL",
        default="postgres://stacos_migrator:stacos_migrator_dev@localhost:5432/stacos",
    ),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["CONN_MAX_AGE"] = 0

STACOS_RLS_ENABLED = env.bool("STACOS_RLS_ENABLED", default=True)

# Fast, deterministic hashing — Argon2 in tests wastes minutes per run.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SMS_PROVIDER = "memory"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# WhiteNoise scans the static dir on startup; skip it in tests.
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m]

# A test app with throwaway scoped models, used to parametrise the isolation
# suite. MIGRATION_MODULES=None keeps `makemigrations --check` from demanding
# migrations for it in CI.
INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]
MIGRATION_MODULES = {"testapp": None}

WAFFLE_CREATE_MISSING_FLAGS = True
WAFFLE_CREATE_MISSING_SWITCHES = True
