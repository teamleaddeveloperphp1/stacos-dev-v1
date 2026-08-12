from django.urls import path

from stacos.obligations import views

app_name = "compliance"

urlpatterns = [
    path("", views.calendar_list, name="calendar"),
    path("month/", views.calendar_month, name="month"),
    path("panel/", views.dashboard_panel, name="dashboard_panel"),
    path("<uuid:pk>/", views.obligation_detail, name="detail"),
    path("<uuid:pk>/transition/", views.obligation_transition, name="transition"),
    path("<uuid:pk>/record-event/", views.record_entity_event, name="record_event"),
    path("entities/<uuid:entity_pk>/rebuild/", views.rebuild_calendar, name="rebuild"),
    path("entities/<uuid:entity_pk>/summary/", views.entity_summary, name="entity_summary"),
    path("definitions/<slug:code>/", views.definition_detail, name="definition"),
]
