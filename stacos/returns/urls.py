from django.urls import path

from stacos.returns import views

app_name = "returns"

urlpatterns = [
    path("", views.preparation_list, name="list"),
    path("<uuid:pk>/", views.preparation_detail, name="detail"),
    path("<uuid:pk>/save/", views.preparation_save, name="save"),
    path("<uuid:pk>/submit/", views.preparation_submit, name="submit"),
    path("<uuid:pk>/review/", views.preparation_review, name="review"),
    path("<uuid:pk>/approve/", views.preparation_approve, name="approve"),
    path("<uuid:pk>/file/", views.preparation_file, name="file"),
    path(
        "<uuid:pk>/differences/<uuid:difference_pk>/resolve/",
        views.difference_resolve,
        name="resolve_difference",
    ),
    path("open/<uuid:obligation_pk>/", views.preparation_open, name="open"),
]
