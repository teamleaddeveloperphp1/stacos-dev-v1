"""
JWT authentication for the mobile client.

**The hole this closes.** A JWT bypasses ``SessionMiddleware`` entirely, so the
dual-OTP gate, step-up freshness and forced-sign-out-on-role-change do *not*
apply to the API by default. Ship it that way and "sign out all devices" quietly
does nothing to the mobile app until the token expires — the single most commonly
shipped hole of this shape.

Three things make it real:

* ``security_epoch`` is a claim, validated on every request. Rotating the user's
  security stamp invalidates every outstanding access token immediately.
* ``verified`` is a claim, so a token minted before dual-OTP completed cannot
  reach tenant data.
* ``auth_time`` is a claim, so step-up freshness can be evaluated for API calls
  the same way it is for the web.
"""

from __future__ import annotations

from typing import Any, cast

from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.tokens import RefreshToken, Token

from stacos.accounts.models import User

__all__ = ["StacosJWTAuthentication", "issue_tokens"]

EPOCH_CLAIM = "sec"
VERIFIED_CLAIM = "vrf"
AUTH_TIME_CLAIM = "auth_time"


def issue_tokens(user: User, *, verified: bool) -> dict[str, Any]:
    """Mint a refresh/access pair carrying STACOS's own claims."""
    refresh = RefreshToken.for_user(user)
    now = int(timezone.now().timestamp())

    for token in (refresh, refresh.access_token):
        token[EPOCH_CLAIM] = str(user.security_stamp)
        token[VERIFIED_CLAIM] = verified
        token[AUTH_TIME_CLAIM] = now

    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "verified": verified,
    }


class StacosJWTAuthentication(JWTAuthentication):
    """Reject tokens whose security epoch no longer matches the user's."""

    def get_user(self, validated_token: Token) -> Any:
        user = cast("User", super().get_user(validated_token))

        epoch = validated_token.get(EPOCH_CLAIM)
        if epoch is None or str(epoch) != str(user.security_stamp):
            raise InvalidToken("This session has been ended. Sign in again.")

        return user
