from django.urls import path

from stacos.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.notification_list, name="list"),
    path("panel/", views.panel, name="panel"),
    path("preferences/", views.preferences, name="preferences"),
    path("read-all/", views.mark_all, name="mark_all"),
    path("<uuid:pk>/read/", views.mark_read, name="mark_read"),
]
