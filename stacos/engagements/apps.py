from django.apps import AppConfig


class EngagementsConfig(AppConfig):
    name = "stacos.engagements"
    label = "engagements"
    verbose_name = "Engagements"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.engagements import permissions_catalog  # noqa: F401
