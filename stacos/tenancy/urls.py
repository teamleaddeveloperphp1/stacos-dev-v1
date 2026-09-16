from django.urls import path

from stacos.tenancy import entity_setup, team, views

app_name = "app"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("entities/", views.EntityListView.as_view(), name="entity_list"),
    path("entities/new/", views.entity_create, name="entity_create"),
    path(
        "entities/registration-fields/",
        views.entity_registration_fields,
        name="entity_registration_fields",
    ),
    path(
        "entities/<uuid:pk>/setup/registrations/",
        entity_setup.registrations,
        name="entity_setup_registrations",
    ),
    path(
        "entities/<uuid:pk>/setup/answers/",
        entity_setup.answers,
        name="entity_setup_answers",
    ),
    path(
        "entities/<uuid:pk>/setup/packs/",
        entity_setup.packs,
        name="entity_setup_packs",
    ),
    path(
        "entities/<uuid:pk>/setup/build/",
        entity_setup.build,
        name="entity_setup_build",
    ),
    path("entities/<uuid:pk>/", views.entity_detail, name="entity_detail"),
    path("entities/<uuid:pk>/edit/", views.entity_edit, name="entity_edit"),
    path("entities/<uuid:pk>/archive/", views.archive_entity, name="entity_archive"),
    path(
        "entities/<uuid:pk>/registrations/new/",
        views.registration_create,
        name="registration_create",
    ),
    path(
        "entities/<uuid:pk>/premises/new/",
        views.premises_create,
        name="premises_create",
    ),
    path("search/", views.palette_search, name="search"),
    path("team/", team.team, name="team"),
    path("team/invite/", team.invite, name="team_invite"),
    path("team/invitations/<uuid:pk>/revoke/", team.invite_revoke, name="team_invite_revoke"),
]
