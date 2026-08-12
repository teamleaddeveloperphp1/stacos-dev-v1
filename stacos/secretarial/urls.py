from django.urls import path

from stacos.secretarial import views

app_name = "secretarial"

urlpatterns = [
    path("meetings/", views.meeting_list, name="meetings"),
    path("meetings/new/", views.meeting_create, name="meeting_create"),
    path("meetings/<uuid:pk>/", views.meeting_detail, name="meeting_detail"),
    path("meetings/<uuid:pk>/attendees/", views.attendee_add, name="attendee_add"),
    path("meetings/<uuid:pk>/hold/", views.meeting_hold, name="meeting_hold"),
    path("resolutions/", views.resolution_list, name="resolutions"),
    path("entities/<uuid:entity_pk>/", views.entity_secretarial, name="entity_summary"),
    path("entities/<uuid:entity_pk>/cap-table/", views.cap_table, name="cap_table"),
]
