from django.urls import path

from stacos.practice import views

app_name = "practice"

urlpatterns = [
    path("", views.board, name="board"),
    path("work/new/", views.work_create, name="work_create"),
    path("work/<uuid:pk>/", views.work_detail, name="work_detail"),
    path("work/<uuid:pk>/move/", views.work_move, name="work_move"),
    path("work/<uuid:pk>/time/", views.time_log, name="time_log"),
    path("profitability/", views.profitability, name="profitability"),
]
