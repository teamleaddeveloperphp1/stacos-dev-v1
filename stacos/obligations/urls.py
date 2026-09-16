from django.urls import path

from stacos.obligations import library_views, views

app_name = "compliance"

urlpatterns = [
    path("", views.calendar_list, name="calendar"),
    path("library/", library_views.entity_picker, name="library_picker"),
    path("library/<uuid:entity_pk>/", library_views.library_detail, name="library"),
    path(
        "library/<uuid:entity_pk>/<slug:code>/remove/",
        library_views.library_remove,
        name="library_remove",
    ),
    path(
        "library/<uuid:entity_pk>/<slug:code>/force-add/",
        library_views.library_force_add,
        name="library_force_add",
    ),
    path(
        "library/<uuid:entity_pk>/<slug:code>/restore/",
        library_views.library_restore,
        name="library_restore",
    ),
    path("bulk/assign/", views.bulk_assign, name="bulk_assign"),
    path("bulk/not-applicable/", views.bulk_not_applicable, name="bulk_not_applicable"),
    path("subscribe/", views.calendar_subscribe, name="subscribe"),
    path("subscribe/create/", views.calendar_subscribe_create, name="subscribe_create"),
    path("subscribe/revoke/", views.calendar_subscribe_revoke, name="subscribe_revoke"),
    path("feed/<str:token>.ics", views.calendar_feed, name="feed"),
    path("month/", views.calendar_month, name="month"),
    path("<uuid:pk>/", views.obligation_detail, name="detail"),
    path("<uuid:pk>/transition/", views.obligation_transition, name="transition"),
    path("<uuid:pk>/complete/", views.obligation_complete_modal, name="complete"),
    path("<uuid:pk>/reopen/", views.obligation_reopen_modal, name="reopen"),
    path("<uuid:pk>/status/", views.obligation_status, name="status"),
    path(
        "<uuid:pk>/acknowledgement/",
        views.obligation_acknowledgement,
        name="acknowledgement",
    ),
    path("<uuid:pk>/assign/", views.obligation_assign, name="assign"),
    path("<uuid:pk>/comment/", views.obligation_comment, name="comment"),
    path("<uuid:pk>/nudge/", views.obligation_nudge, name="nudge"),
    path("steps/<uuid:pk>/toggle/", views.obligation_step_toggle, name="step_toggle"),
    path("steps/<uuid:pk>/block/", views.obligation_step_block, name="step_block"),
    path("steps/<uuid:pk>/assign/", views.obligation_step_assign, name="step_assign"),
    path("steps/<uuid:pk>/nudge/", views.obligation_step_nudge, name="step_nudge"),
    path("<uuid:pk>/record-event/", views.record_entity_event, name="record_event"),
    path("<uuid:pk>/confirm/", views.confirm_obligation, name="confirm"),
    path("entities/<uuid:entity_pk>/rebuild/", views.rebuild_calendar, name="rebuild"),
    path("entities/<uuid:entity_pk>/events/", views.entity_events, name="entity_events"),
    path("entities/<uuid:entity_pk>/events/new/", views.event_create, name="event_create"),
    path("events/<uuid:pk>/withdraw/", views.event_withdraw, name="event_withdraw"),
    path("entities/<uuid:entity_pk>/summary/", views.entity_summary, name="entity_summary"),
    path(
        "entities/<uuid:entity_pk>/answer/<slug:fact_key>/",
        views.answer_entity_question,
        name="answer_entity_question",
    ),
    path(
        "entities/<uuid:entity_pk>/packs/<slug:code>/",
        views.toggle_entity_pack,
        name="toggle_entity_pack",
    ),
    path("definitions/<slug:code>/", views.definition_detail, name="definition"),
]
