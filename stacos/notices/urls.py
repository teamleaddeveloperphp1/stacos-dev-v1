from django.urls import path

from stacos.notices import views

app_name = "notices"

urlpatterns = [
    path("", views.notice_list, name="list"),
    path("new/", views.notice_create, name="create"),
    path("<uuid:pk>/", views.notice_detail, name="detail"),
    path("<uuid:pk>/transition/", views.notice_transition, name="transition"),
    path("<uuid:pk>/respond/", views.notice_respond, name="respond"),
    path("<uuid:pk>/close/", views.notice_close, name="close"),
    path("for-entity/<uuid:entity_pk>/", views.entity_notices, name="for_entity"),
]
