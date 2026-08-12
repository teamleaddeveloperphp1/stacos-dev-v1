from django.apps import AppConfig


class TestAppConfig(AppConfig):
    """Throwaway models used to exercise the scoping machinery directly.

    Installed only under ``config.settings.test``, with ``MIGRATION_MODULES``
    mapping it to ``None`` so ``makemigrations --check`` in CI does not demand
    migrations for it.
    """

    name = "tests.testapp"
    label = "testapp"
    default_auto_field = "django.db.models.BigAutoField"
