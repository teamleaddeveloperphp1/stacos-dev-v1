from django.apps import AppConfig


class NoticesConfig(AppConfig):
    name = "stacos.notices"
    label = "notices"
    verbose_name = "Notices"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.notices import permissions_catalog  # noqa: F401
