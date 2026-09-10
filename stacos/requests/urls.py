from django.urls import path

from stacos.requests import responder, views

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
    path("<uuid:pk>/invite/", views.request_invite_responder, name="invite_responder"),
]

#: The outside contact's routes, mounted at the site root rather than under
#: ``/app/``. Everything under ``/app/`` assumes a session and a membership — the
#: organisation gate redirects anybody without one — and the holder of a
#: responder link has neither by definition.
responder_urlpatterns = [
    path("respond/<str:token>/", responder.respond, name="responder"),
    path(
        "respond/<str:token>/items/<uuid:item_pk>/",
        responder.respond_item,
        name="responder_item",
    ),
    path(
        "respond/<str:token>/items/<uuid:item_pk>/upload/",
        responder.upload_item,
        name="responder_upload",
    ),
]
