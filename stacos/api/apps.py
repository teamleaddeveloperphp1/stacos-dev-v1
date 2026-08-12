from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "stacos.api"
    label = "api"
    verbose_name = "API"
    default_auto_field = "django.db.models.BigAutoField"
