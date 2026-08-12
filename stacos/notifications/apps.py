from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "stacos.notifications"
    label = "notifications"
    verbose_name = "Notifications"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # `templates_registry` registers the WhatsApp templates this module
        # sends, so the account's whole inventory is enumerable at startup rather
        # than discovered when a send fails.
        from stacos.notifications import (
            permissions_catalog,  # noqa: F401
            templates_registry,  # noqa: F401
        )
