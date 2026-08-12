from django.apps import AppConfig


class PracticeConfig(AppConfig):
    name = "stacos.practice"
    label = "practice"
    verbose_name = "Practice management"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.practice import permissions_catalog  # noqa: F401
