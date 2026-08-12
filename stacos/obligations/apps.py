from django.apps import AppConfig


class ObligationsConfig(AppConfig):
    name = "stacos.obligations"
    label = "obligations"
    verbose_name = "Obligations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.obligations import permissions_catalog  # noqa: F401
