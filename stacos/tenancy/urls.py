from django.urls import path

from stacos.tenancy import views

app_name = "app"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("entities/", views.EntityListView.as_view(), name="entity_list"),
    path("entities/new/", views.entity_create, name="entity_create"),
    path("entities/<uuid:pk>/", views.entity_detail, name="entity_detail"),
    path("entities/<uuid:pk>/edit/", views.entity_edit, name="entity_edit"),
    path("entities/<uuid:pk>/archive/", views.archive_entity, name="entity_archive"),
    path(
        "entities/<uuid:pk>/registrations/new/",
        views.registration_create,
        name="registration_create",
    ),
    path("search/", views.palette_search, name="search"),
    path("switch-tenant/", views.switch_tenant, name="switch_tenant"),
]
