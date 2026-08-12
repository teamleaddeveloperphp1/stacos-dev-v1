from django.apps import AppConfig


class VaultConfig(AppConfig):
    name = "stacos.vault"
    label = "vault"
    verbose_name = "Document vault"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.vault import permissions_catalog  # noqa: F401
