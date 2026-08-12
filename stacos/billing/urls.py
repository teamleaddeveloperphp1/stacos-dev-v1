from django.urls import path

from stacos.billing import views

app_name = "billing"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("plans/", views.plans, name="plans"),
    path("invoices/issue/", views.invoice_issue, name="invoice_issue"),
    path("invoices/<uuid:pk>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<uuid:pk>/payment/", views.payment_record, name="payment_record"),
    path("invoices/<uuid:pk>/void/", views.invoice_void, name="invoice_void"),
]
