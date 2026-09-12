"""
Base settings shared by every environment.

Environment-specific modules (dev, prod, test) import * from here and override.
Nothing in this file may assume a particular environment.
"""

from datetime import timedelta
from pathlib import Path

import django_stubs_ext
import environ

# Lets `ListView[Entity]` and `QuerySet[Entity]` be written as real annotations
# rather than only inside `if TYPE_CHECKING`. Must run before any view or model
# module is imported.
django_stubs_ext.monkeypatch()

# ---------------------------------------------------------------------------
# Paths and environment
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-override-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
BASE_URL = env("DJANGO_BASE_URL", default="http://localhost:8000")

# ---------------------------------------------------------------------------
# Applications
#
# django-cotton's loader must be able to see templates/components. Third-party
# apps that render templates are listed before our own so our overrides win.
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "django.contrib.humanize",
    # Serves /sitemap.xml for the public surface. No django.contrib.sites: the
    # framework falls back to the requesting host, which is what we want for a
    # single-domain deployment.
    "django.contrib.sitemaps",
]

THIRD_PARTY_APPS = [
    "django_cotton",
    "django_htmx",
    "crispy_forms",
    "crispy_bootstrap5",
    "rest_framework",
    "drf_spectacular",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.microsoft",
    "allauth.socialaccount.providers.apple",
    "django_celery_beat",
    "waffle",
]

LOCAL_APPS = [
    "stacos.core",
    "stacos.accounts",
    "stacos.jurisdictions",
    "stacos.tenancy",
    "stacos.engagements",
    # The catalog is platform-owned reference data; the register is what each
    # tenant owes. Catalog first, because obligations reference its models.
    "stacos.catalog",
    "stacos.obligations",
    "stacos.notifications",
    "stacos.practice",
    # Billing before dealers: a commission is a share of an invoice.
    "stacos.billing",
    "stacos.dealers",
    "stacos.marketing",
    "stacos.api",
    "stacos.platformadmin",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Middleware — the ordering here is load-bearing.
#
#   RequestContextMiddleware  must run early and CLEAR structlog contextvars at
#                             request start. Binding without clearing leaks
#                             tenant_id into the next request on a reused worker
#                             thread, which in a compliance product is an
#                             audit-integrity problem, not just noisy logs.
#   SecurityStamp / Verification gates run after authentication, before anything
#                             that reads tenant data.
#   ScopeMiddleware           binds the AccessScope and issues `SET LOCAL` for
#                             Row-Level Security inside an explicit transaction.
#                             It must come after auth and before any view.
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "stacos.core.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # Above the two gates below, not beneath them. It only reads request headers,
    # and both gates ask `request.htmx` before deciding between a 302 and an
    # HX-Redirect. Listed after them, that attribute does not exist yet, the
    # question silently answers "no", and an expired session gets a redirect HTMX
    # follows and swaps — leaving the user staring at an unchanged screen.
    "django_htmx.middleware.HtmxMiddleware",
    "stacos.accounts.middleware.SecurityStampMiddleware",
    "stacos.accounts.middleware.VerificationGateMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "stacos.core.middleware.ScopeMiddleware",
    # After ScopeMiddleware, so `request.tenant` is resolved, and before the
    # authorisation middleware, so a user who belongs to no organisation is sent
    # to the setup flow instead of being shown a 403 for a permission only a
    # member could hold. Deciding it here is what makes it hold for pages that
    # do not exist yet.
    "stacos.tenancy.middleware.OrganisationGateMiddleware",
    # Innermost, so it is the first to see an exception raised by a view and can
    # turn an authorisation failure into a redirect or a 403 rather than a 500.
    "stacos.core.middleware.AuthorizationExceptionMiddleware",
    "waffle.middleware.WaffleMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "stacos.core.context_processors.stacos_context",
                # Public navigation and footer. Module constants only — no
                # queries, so it is cheap enough to run on every render.
                "stacos.marketing.context_processors.marketing_chrome",
                # One count on a partial index. It guards itself against running
                # outside a tenant scope — see the module docstring.
                "stacos.notifications.context_processors.notification_badge",
            ],
            # django-cotton's loader must precede the app-directories loader.
            # In production these are wrapped by the cached loader (see prod.py).
            "loaders": [
                "django_cotton.cotton_loader.Loader",
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ],
            "builtins": [
                "django_cotton.templatetags.cotton",
                "stacos.core.templatetags.stacos",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database
#
# Two roles by design: the app connects as `stacos_app` (NOSUPERUSER,
# NOBYPASSRLS) so Row-Level Security applies; migrations run as the schema
# owner. `tasks.ps1 migrate` swaps DATABASE_URL for DATABASE_MIGRATE_URL.
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default="postgres://stacos_app:stacos_app_dev@localhost:5432/stacos",
    ),
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DATABASE_CONN_MAX_AGE", default=60)
DATABASES["default"]["ATOMIC_REQUESTS"] = False  # ScopeMiddleware opens its own transaction

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Row-Level Security. Disable only to debug a policy problem, never in production.
STACOS_RLS_ENABLED = env.bool("STACOS_RLS_ENABLED", default=True)

# ---------------------------------------------------------------------------
# Cache, sessions, Celery
# ---------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    },
}

SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_COOKIE_NAME = "stacos_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/2")

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Argon2 first; the rest remain so existing hashes still verify.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "/app/"
LOGOUT_REDIRECT_URL = "/"

# django-allauth >= 65. The pre-65 setting names are silently ignored, which is a
# security-relevant misconfiguration rather than a warning — so they are asserted
# in tests rather than merely set here.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "none"  # STACOS runs its own dual-OTP verification
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_ADAPTER = "stacos.accounts.adapters.StacosAccountAdapter"
SOCIALACCOUNT_ADAPTER = "stacos.accounts.adapters.StacosSocialAccountAdapter"
SOCIALACCOUNT_EMAIL_VERIFICATION = "none"
SOCIALACCOUNT_STORE_TOKENS = False

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": env("GOOGLE_CLIENT_ID", default=""),
            "secret": env("GOOGLE_CLIENT_SECRET", default=""),
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    },
    "microsoft": {
        "APP": {
            "client_id": env("MICROSOFT_CLIENT_ID", default=""),
            "secret": env("MICROSOFT_CLIENT_SECRET", default=""),
        },
        "TENANT": "common",
    },
    "apple": {
        "APP": {
            "client_id": env("APPLE_CLIENT_ID", default=""),
            "secret": env("APPLE_SECRET", default=""),
            "key": env("APPLE_KEY_ID", default=""),
            "settings": {"certificate_key": env("APPLE_CERTIFICATE_KEY", default="")},
        },
    },
}

# --- STACOS dual-OTP, device trust and step-up ---------------------------------
# Both an email OTP and a phone OTP must be satisfied together, in one step, to
# complete registration and to complete login on an unrecognised device.
# Social login proves the provider identity and satisfies NEITHER.
STACOS_OTP = {
    "CODE_LENGTH": 6,
    "CODE_TTL_SECONDS": env.int("OTP_CODE_TTL_SECONDS", default=600),
    "MAX_ATTEMPTS": 5,
    "PEPPER": env("OTP_PEPPER", default="dev-only-otp-pepper-change-me"),
    "MAX_PER_IDENTITY_PER_HOUR": env.int("OTP_MAX_PER_IDENTITY_PER_HOUR", default=5),
    "MAX_PER_IDENTITY_PER_DAY": env.int("OTP_MAX_PER_IDENTITY_PER_DAY", default=10),
    "MAX_PER_IP_PER_HOUR": env.int("OTP_MAX_PER_IP_PER_HOUR", default=20),
    "RESEND_BACKOFF_SECONDS": [30, 60, 120, 300, 600],
}

#: The channel the phone-side code is delivered over. WhatsApp, not SMS.
OTP_PHONE_CHANNEL = "whatsapp"

TRUSTED_DEVICE_DAYS = env.int("TRUSTED_DEVICE_DAYS", default=30)
TRUSTED_DEVICE_COOKIE = "stacos_td"
STEP_UP_MAX_AGE_SECONDS = env.int("STEP_UP_MAX_AGE_SECONDS", default=600)

# --- WhatsApp -----------------------------------------------------------------
# The second verification channel. WhatsApp rather than SMS because for Indian
# businesses it is the channel people actually read.
#
# `TEMPLATE_APPROVAL` is not a setting, it is a reminder: business-initiated
# messages require templates pre-approved by Meta, one-time passcodes must use
# the AUTHENTICATION category, and approval takes days to weeks per WhatsApp
# Business Account. It blocks exactly the way DLT registration blocks SMS.
WHATSAPP = {
    "PROVIDER": env("WHATSAPP_PROVIDER", default="console"),
    "ACCESS_TOKEN": env("WHATSAPP_ACCESS_TOKEN", default=""),
    "PHONE_NUMBER_ID": env("WHATSAPP_PHONE_NUMBER_ID", default=""),
    "BUSINESS_ACCOUNT_ID": env("WHATSAPP_BUSINESS_ACCOUNT_ID", default=""),
    "API_VERSION": env("WHATSAPP_API_VERSION", default="v21.0"),
    # Indicative per-message cost for an authentication conversation, used by the
    # daily spend cap. Reconciled against Meta's billing webhook.
    "COST_PER_MESSAGE": env("WHATSAPP_COST_PER_MESSAGE", default="0.125"),
    "DAILY_SPEND_CAP_UNITS": env.int("WHATSAPP_DAILY_SPEND_CAP_UNITS", default=1000),
}

# ---------------------------------------------------------------------------
# Internationalisation
#
# Storage stays UTC. IST is the display default because Indian statutory
# reporting means IST when it says "a day". Per-jurisdiction timezones are
# resolved at the model level, never from this setting.
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

# ---------------------------------------------------------------------------
# Static and media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# ---------------------------------------------------------------------------
# Forms
#
# The boundary: crispy renders form FIELDS, django-cotton renders everything
# else. Ambiguity here produces two design systems.
# ---------------------------------------------------------------------------
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

# django-cotton components live in templates/components/ rather than the default
# templates/cotton/, so the design system sits where a developer looks for it.
COTTON_DIR = "components"

# ---------------------------------------------------------------------------
# DRF — narrow by design. It exists for the mobile client and webhooks, not for
# the web UI. The web app talks HTML.
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Validates the security-epoch claim, so rotating a user's security stamp
        # invalidates outstanding mobile tokens immediately rather than at expiry.
        "stacos.api.authentication.StacosJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_RATES": {"anon": "60/hour", "user": "1000/hour"},
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

# Short access tokens, revocable refresh tokens. Both matter: the epoch claim
# makes revocation immediate, and a short access lifetime bounds the window in
# which a leaked token is useful.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "STACOS API",
    "DESCRIPTION": "Mobile client and webhook API. The web application renders HTML and does not use this API.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
}

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="STACOS <no-reply@stacos.local>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# ---------------------------------------------------------------------------
# Marketing surface
#
# Indexing is opt-in per deployment. A staging copy of the site indexed by a
# search engine competes with production for its own keywords and leaks
# unreleased copy, so the default is "no" and production says otherwise.
# ---------------------------------------------------------------------------
MARKETING_ALLOW_INDEXING = env.bool("MARKETING_ALLOW_INDEXING", default=False)

#: Enquiry routing. Keyed by the topic a visitor chooses on the contact form;
#: `default` catches anything unrecognised, including a tampered payload.
MARKETING_ENQUIRY_INBOX = {
    "default": env("MARKETING_INBOX_SALES", default="sales@stacos.local"),
    "sales": env("MARKETING_INBOX_SALES", default="sales@stacos.local"),
    "demo": env("MARKETING_INBOX_SALES", default="sales@stacos.local"),
    "support": env("MARKETING_INBOX_SUPPORT", default="support@stacos.local"),
    "security": env("MARKETING_INBOX_SECURITY", default="security@stacos.local"),
    "partner": env("MARKETING_INBOX_PARTNERS", default="partners@stacos.local"),
    "press": env("MARKETING_INBOX_PRESS", default="press@stacos.local"),
}

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
WAFFLE_CREATE_MISSING_FLAGS = False
WAFFLE_CREATE_MISSING_SWITCHES = False

# ---------------------------------------------------------------------------
# Logging — structlog with request/tenant correlation.
# ---------------------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=False)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)-7s %(name)s  %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "stacos": {"level": LOG_LEVEL, "propagate": True},
    },
}
