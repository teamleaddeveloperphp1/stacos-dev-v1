from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "stacos.billing"
    label = "billing"
    verbose_name = "Billing"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.billing import permissions_catalog  # noqa: F401
