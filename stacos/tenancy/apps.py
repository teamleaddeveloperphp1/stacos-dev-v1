from django.apps import AppConfig


class TenancyConfig(AppConfig):
    name = "stacos.tenancy"
    label = "tenancy"
    verbose_name = "Tenancy"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.tenancy import permissions_catalog  # noqa: F401
