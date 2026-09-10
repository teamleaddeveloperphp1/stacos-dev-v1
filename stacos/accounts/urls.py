from django.urls import path

from stacos.accounts import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    # The dual-OTP screen: both codes, one submission.
    path("verify/", views.verify, name="verify"),
    path("verify/resend/", views.resend, name="resend"),
    path("step-up/", views.step_up, name="step_up"),
    path("security/", views.security_settings, name="security"),
    path("security/revoke-all/", views.revoke_devices, name="revoke_devices"),
    # Under /auth/, not /app/: an invitee has no membership yet, and everything
    # behind /app/ redirects somebody in that position into organisation setup.
    path("invite/<str:token>/", views.accept_invitation, name="accept_invitation"),
]
