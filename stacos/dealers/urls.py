from django.urls import path

from stacos.dealers import views

app_name = "dealers"

urlpatterns = [
    path("", views.commission_statement, name="statement"),
    path("payouts/<uuid:pk>/", views.payout_detail, name="payout_detail"),
    path("payouts/<uuid:pk>/approve/", views.payout_approve, name="payout_approve"),
    path("payouts/<uuid:pk>/pay/", views.payout_pay, name="payout_pay"),
]
