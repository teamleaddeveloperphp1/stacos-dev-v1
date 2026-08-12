from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "stacos.core"
    label = "core"
    verbose_name = "Core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Registers this app's permissions and the system checks that keep the
        # tenancy and permission conventions honest.
        from stacos.core import (
            checks,  # noqa: F401
            permissions_catalog,  # noqa: F401
        )
