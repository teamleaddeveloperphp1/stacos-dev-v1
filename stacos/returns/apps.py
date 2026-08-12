from django.apps import AppConfig


class ReturnsConfig(AppConfig):
    name = "stacos.returns"
    label = "returns"
    verbose_name = "Return preparation"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.returns import permissions_catalog  # noqa: F401
