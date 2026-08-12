from django.apps import AppConfig


class PlatformAdminConfig(AppConfig):
    name = "stacos.platformadmin"
    label = "platformadmin"
    verbose_name = "Platform administration"
    default_auto_field = "django.db.models.BigAutoField"
