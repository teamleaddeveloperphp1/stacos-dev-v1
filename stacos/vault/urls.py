from django.urls import path

from stacos.vault import views

app_name = "vault"

urlpatterns = [
    path("", views.document_list, name="list"),
    path("upload/", views.document_upload, name="upload"),
    path("attachments/", views.attachments, name="attachments"),
    path("<uuid:pk>/", views.document_detail, name="detail"),
    path("<uuid:pk>/download/", views.document_download, name="download"),
    path("<uuid:pk>/archive/", views.document_archive, name="archive"),
]
