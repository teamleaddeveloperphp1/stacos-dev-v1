"""
API endpoints for the mobile client.

Auth only, in the foundation. The mobile app is for *acting and responding* —
approving a return, answering an information request, closing an internal
compliance with a photograph — so its feature endpoints belong with the modules
that own those objects, not here.

Every rule the mobile client enforces lives in a shared Python service the web
views call too. There is no mobile-specific business logic anywhere.
"""

from __future__ import annotations

import contextlib
from typing import Any

import structlog
from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from stacos.accounts.models import PendingVerification
from stacos.accounts.otp import start_verification, verify_codes
from stacos.api.authentication import issue_tokens
from stacos.core.rls import rls_bootstrap
from stacos.core.typing import current_user
from stacos.tenancy.models import Membership

logger = structlog.get_logger(__name__)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class VerifySerializer(serializers.Serializer):
    verification_id = serializers.UUIDField()
    email_code = serializers.CharField(max_length=8)
    phone_code = serializers.CharField(max_length=8)


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class MobileLoginView(APIView):
    """Step one: password, then always a dual-OTP challenge.

    The mobile client has no trusted-device cookie, so every sign-in from the app
    is treated as a new device. Device trust on mobile is the OS keychain holding
    the refresh token, which is a stronger guarantee than a browser cookie.
    """

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []

    @extend_schema(
        request=LoginSerializer,
        responses={
            200: OpenApiResponse(description="Verification challenge issued."),
            401: OpenApiResponse(description="Invalid credentials."),
            429: OpenApiResponse(description="Rate limited."),
        },
    )
    def post(self, request: Request) -> Response:
        from django.contrib.auth import authenticate

        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=serializer.validated_data["email"].lower(),
            password=serializer.validated_data["password"],
        )
        if user is None or not user.is_active:
            return Response(
                {"detail": "Email or password is incorrect."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            verification, decision = start_verification(
                purpose=PendingVerification.Purpose.LOGIN_NEW_DEVICE,
                email=user.email,
                phone_e164=user.phone_e164,
                user=user,
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.headers.get("User-Agent", ""),
            )
        except Exception:
            # Same failure mode as the web login (`accounts.views
            # ._start_verification_safely`): the password is already checked,
            # so an outage in the mail or WhatsApp provider must read as
            # "try again", never as a stack trace in a JSON body a mobile
            # client cannot parse into anything useful.
            logger.exception("api.verification_dispatch_failed", user_id=str(user.pk))
            return Response(
                {
                    "detail": "We could not send your verification codes just now. Try again shortly."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if verification is None:
            return Response(
                {"detail": decision.reason, "retry_after": decision.retry_after_seconds},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        return Response(
            {
                "verification_id": str(verification.id),
                "expires_at": verification.expires_at,
                "message": "Enter the codes sent to your email and your phone.",
            }
        )


class MobileVerifyView(APIView):
    """Step two: both codes together, then tokens."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []

    @extend_schema(
        request=VerifySerializer, responses={200: OpenApiResponse(description="Tokens.")}
    )
    def post(self, request: Request) -> Response:
        serializer = VerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        verification = PendingVerification.objects.filter(
            pk=serializer.validated_data["verification_id"],
            status=PendingVerification.Status.PENDING,
        ).first()
        if verification is None:
            return Response(
                {"detail": "This verification is no longer valid. Start again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        outcome = verify_codes(
            verification,
            email_code=serializer.validated_data["email_code"],
            phone_code=serializer.validated_data["phone_code"],
        )
        if not outcome.ok:
            return Response(
                {
                    "detail": outcome.error or "One or both codes are incorrect.",
                    "email_error": outcome.email_error,
                    "phone_error": outcome.phone_error,
                    "locked_out": outcome.locked_out,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = verification.user
        assert user is not None
        user.refresh_from_db()
        return Response(issue_tokens(user, verified=True))


class MobileTokenRefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []

    @extend_schema(
        request=RefreshSerializer, responses={200: OpenApiResponse(description="Tokens.")}
    )
    def post(self, request: Request) -> Response:
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            refresh = RefreshToken(serializer.validated_data["refresh"])
        except TokenError:
            return Response(
                {"detail": "Invalid refresh token."}, status=status.HTTP_401_UNAUTHORIZED
            )

        from stacos.accounts.models import User
        from stacos.api.authentication import EPOCH_CLAIM

        user = User.objects.filter(pk=refresh.get("user_id")).first()
        if user is None or str(refresh.get(EPOCH_CLAIM)) != str(user.security_stamp):
            # The stamp rotated: the user signed out everywhere, or their role
            # changed. Refresh must fail, not silently mint a new access token.
            return Response(
                {"detail": "This session has been ended. Sign in again."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(issue_tokens(user, verified=bool(refresh.get("vrf", False))))


class MobileLogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=RefreshSerializer, responses={204: OpenApiResponse(description="Done.")})
    def post(self, request: Request) -> Response:
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Blacklisting needs simplejwt's optional app; rotating the security
        # stamp is the reliable revocation path either way, so a failure here is
        # not worth surfacing to the client.
        with contextlib.suppress(TokenError, AttributeError):
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """Who am I, and which tenants can I switch between.

    The tenant ids returned here are what the client puts in ``X-Stacos-Tenant``
    on subsequent requests — a JWT request has no session to hold the switcher's
    choice, so it travels per request.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Current user and memberships.")})
    def get(self, request: Request) -> Response:
        user = current_user(request)

        # The same chicken-and-egg the web request path has: "which tenants may
        # this user reach" is answered by reading a table that is itself
        # RLS-protected, and no scope is bound yet. Without the bootstrap the
        # policy fails closed and this endpoint reports, with a 200, that the
        # user belongs to nothing.
        #
        # The transaction is needed because the setting is transaction-local,
        # and the lookup is anchored on the authenticated user, so lifting the
        # policy here can only ever return rows about them.
        with transaction.atomic(), rls_bootstrap():
            memberships = list(
                Membership.objects_unscoped.filter(user=user, status=Membership.Status.ACTIVE)
                .select_related("tenant", "role")
                .order_by("tenant__name")
            )

        return Response(
            {
                "id": str(user.pk),
                "email": user.email,
                "full_name": user.full_name,
                "initials": user.initials,
                "verified": user.is_fully_verified,
                "tenants": [
                    {
                        "id": str(m.tenant_id),
                        "name": m.tenant.name,
                        "type": m.tenant.type,
                        "role": m.role.name,
                    }
                    for m in memberships
                ],
            }
        )
