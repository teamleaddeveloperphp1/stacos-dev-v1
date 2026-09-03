from django.urls import path

from stacos.tenancy.onboarding import views

app_name = "onboarding"

urlpatterns = [
    path("", views.identity, name="identity"),
    path("decode/", views.identity_decode, name="identity_decode"),
    path("profile/", views.profile, name="profile"),
    path("profile/answer/<slug:fact_key>/", views.answer_question, name="answer"),
    path("preview/", views.preview, name="preview"),
    path("preview/packs/<slug:code>/", views.toggle_pack, name="toggle_pack"),
    path("finish/", views.finish, name="finish"),
]
