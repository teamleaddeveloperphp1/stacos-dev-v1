from django.apps import AppConfig


class SecretarialConfig(AppConfig):
    name = "stacos.secretarial"
    label = "secretarial"
    verbose_name = "Corporate secretarial"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.secretarial import permissions_catalog  # noqa: F401
