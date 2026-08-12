from django.apps import AppConfig


class RequestsConfig(AppConfig):
    # The Python package is `stacos.requests`; the app label is `rfi` so that
    # nothing in a template or a query ever has to disambiguate it from Django's
    # `request` object or the `requests` library.
    name = "stacos.requests"
    label = "rfi"
    verbose_name = "Information requests"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from stacos.requests import permissions_catalog  # noqa: F401
