from django.urls import path

from stacos.marketing import views

app_name = "marketing"

urlpatterns = [
    path("", views.home, name="home"),
    path("pricing/", views.pricing, name="pricing"),
    path("security/", views.security, name="security"),
]
