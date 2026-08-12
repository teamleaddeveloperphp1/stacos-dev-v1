from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "stacos.accounts"
    label = "accounts"
    verbose_name = "Accounts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.accounts import (
            permissions_catalog,  # noqa: F401
            signals,  # noqa: F401
        )
