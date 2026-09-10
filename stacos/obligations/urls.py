from django.urls import path

from stacos.obligations import views

app_name = "compliance"

urlpatterns = [
    path("", views.calendar_list, name="calendar"),
    path("month/", views.calendar_month, name="month"),
    path("<uuid:pk>/", views.obligation_detail, name="detail"),
    path("<uuid:pk>/transition/", views.obligation_transition, name="transition"),
    path("<uuid:pk>/record-event/", views.record_entity_event, name="record_event"),
    path("<uuid:pk>/confirm/", views.confirm_obligation, name="confirm"),
    path("entities/<uuid:entity_pk>/rebuild/", views.rebuild_calendar, name="rebuild"),
    path("entities/<uuid:entity_pk>/events/", views.entity_events, name="entity_events"),
    path("entities/<uuid:entity_pk>/events/new/", views.event_create, name="event_create"),
    path("events/<uuid:pk>/withdraw/", views.event_withdraw, name="event_withdraw"),
    path("entities/<uuid:entity_pk>/summary/", views.entity_summary, name="entity_summary"),
    path("definitions/<slug:code>/", views.definition_detail, name="definition"),
]
