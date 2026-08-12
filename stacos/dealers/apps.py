from django.apps import AppConfig


class DealersConfig(AppConfig):
    name = "stacos.dealers"
    label = "dealers"
    verbose_name = "Channel partners"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.dealers import permissions_catalog  # noqa: F401
