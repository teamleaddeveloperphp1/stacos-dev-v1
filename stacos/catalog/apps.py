from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "stacos.catalog"
    label = "catalog"
    verbose_name = "Compliance catalog"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.catalog import permissions_catalog  # noqa: F401
