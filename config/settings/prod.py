"""Production settings."""

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration

from .base import *
from .base import BASE_DIR, TEMPLATES, env

DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# ---------------------------------------------------------------------------
# Transport security
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# ---------------------------------------------------------------------------
# Templates: wrap the loaders in the cached loader.
#
# django-cotton's loader must stay first inside the cached loader, not outside
# it — getting this wrong is a bug class that only appears once DEBUG=False, so
# CI runs a prod-settings smoke test.
# ---------------------------------------------------------------------------
TEMPLATES[0]["OPTIONS"]["loaders"] = [
    (
        "django.template.loaders.cached.Loader",
        [
            "django_cotton.cotton_loader.Loader",
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ],
    )
]

# ---------------------------------------------------------------------------
# Storage — S3-compatible, in an Indian region for DPDP data residency.
# No public buckets, presigned URLs only, no file ever served through Django.
# ---------------------------------------------------------------------------
if env.bool("USE_S3", default=True):
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": env("AWS_STORAGE_BUCKET_NAME"),
                "region_name": env("AWS_S3_REGION_NAME", default="ap-south-1"),
                "endpoint_url": env("AWS_S3_ENDPOINT_URL", default=None),
                "default_acl": "private",
                "querystring_auth": True,
                "querystring_expire": 300,
                "file_overwrite": False,
                "object_parameters": {"ServerSideEncryption": "AES256"},
            },
        },
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = True

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
LOG_JSON = True

if env("SENTRY_DSN", default=""):
    sentry_sdk.init(
        dsn=env("SENTRY_DSN"),
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),
        environment=env("SENTRY_ENVIRONMENT", default="production"),
        release=env("SENTRY_RELEASE", default=None),
        # Tax IDs, phone numbers and document contents must never reach Sentry.
        send_default_pii=False,
    )

LOCALE_PATHS = [BASE_DIR / "locale"]
