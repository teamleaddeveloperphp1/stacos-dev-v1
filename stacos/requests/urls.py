from django.urls import path

from stacos.requests import views

app_name = "rfi"

urlpatterns = [
    path("", views.request_list, name="list"),
    path("new/", views.request_create, name="create"),
    path("<uuid:pk>/", views.request_detail, name="detail"),
    path("<uuid:pk>/send/", views.request_send, name="send"),
    path("<uuid:pk>/close/", views.request_close, name="close"),
    path("<uuid:pk>/items/<uuid:item_pk>/respond/", views.item_respond, name="item_respond"),
    path("<uuid:pk>/items/<uuid:item_pk>/reject/", views.item_reject, name="item_reject"),
    path("for-obligation/<uuid:obligation_pk>/", views.obligation_requests, name="for_obligation"),
]
