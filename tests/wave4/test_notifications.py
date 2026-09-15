"""
Notifications: raising, deduplicating, delivering, and the reminder ladder.

The three failures worth testing here are the ones that make a notification
system worse than none at all:

1. **Sending twice.** A sweep runs hourly; without idempotence the first time a
   task is redelivered, every client the firm has gets the same reminder again.
2. **Telling the wrong people.** A plant HR user with two entities being told
   about a group company's GST return is a data leak wearing a helpful hat.
3. **Going quiet on the thing that matters.** A user who chose a daily digest
   must still hear *today* that a notice expires tomorrow — batching an urgent
   statutory deadline is how the product becomes responsible for a missed one.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.scope import platform_scope, tenant_context
from stacos.notifications.models import (
    Channel,
    Delivery,
    DeliveryState,
    DigestFrequency,
    Notification,
    NotificationKind,
    NotificationPreference,
    Severity,
)
from stacos.notifications.services import (
    preferences_for,
    raise_notification,
    unread_count,
)
from stacos.notifications.tasks import (
    LADDER,
    _rung,
    deliver_notification,
    sweep_obligation_reminders,
)
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


@pytest.fixture
def org_member(org: Tenant) -> User:
    """A second person in the same tenant, to prove a notification is addressed."""
    from tests.conftest import _make_member

    return _make_member(org, "member@acme.example", "Ravi Kumar", "+919800000009", "org-owner")


def _raise(org: Tenant, user: User, **overrides: object) -> object:
    payload = {
        "tenant_id": org.pk,
        "recipient": user,
        "kind": NotificationKind.OBLIGATION_DUE,
        "title": "GSTR-3B is due 20 Sep",
        "dedupe_key": "obligation:abc:T-3",
        "severity": Severity.ATTENTION,
    }
    payload.update(overrides)
    return raise_notification(**payload)  # type: ignore[arg-type]


# ===========================================================================
# Raising, and not raising twice
# ===========================================================================


def test_raising_a_notification_creates_the_in_app_record(org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        result = _raise(org, org_owner)

        assert result.created
        assert Notification.objects.filter(recipient=org_owner).count() == 1
        # The in-app delivery is the row itself, so it is already sent.
        assert (
            Delivery.objects.filter(
                notification=result.notification, channel=Channel.IN_APP
            ).first()
            or Delivery(state="")
        ).state == DeliveryState.SENT


def test_the_same_dedupe_key_is_raised_once(org: Tenant, org_owner: User) -> None:
    """The property that makes an hourly sweep safe.

    Without it, the first redelivered message re-sends to every recipient in the
    tenant, and the second one teaches them all to filter the sender.
    """
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        first = _raise(org, org_owner)
        second = _raise(org, org_owner)

        assert first.created
        assert not second.created
        assert Notification.objects.filter(recipient=org_owner).count() == 1


def test_a_different_rung_is_a_different_notification(org: Tenant, org_owner: User) -> None:
    """T-7 and T-3 are separate messages about the same obligation, by design."""
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        _raise(org, org_owner, dedupe_key="obligation:abc:T-7")
        _raise(org, org_owner, dedupe_key="obligation:abc:T-3")

        assert Notification.objects.filter(recipient=org_owner).count() == 2


def test_an_inactive_recipient_is_not_notified(org: Tenant, org_owner: User) -> None:
    """A badge nobody will ever clear is worse than no badge."""
    org_owner.is_active = False

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        assert not _raise(org, org_owner).created
        assert Notification.objects.count() == 0


# ===========================================================================
# Channels and preferences
# ===========================================================================


def test_email_is_planned_and_sent(org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        result = _raise(org, org_owner)
        deliver_notification(tenant_id=str(org.pk), notification_id=str(result.notification.pk))

        row = Delivery.objects.get(notification=result.notification, channel=Channel.EMAIL)

    assert row.state == DeliveryState.SENT
    assert row.destination == org_owner.email
    assert len(mail.outbox) == 1
    assert "GSTR-3B" in mail.outbox[0].subject


def test_muting_a_kind_suppresses_the_email_but_not_the_record(
    org: Tenant, org_owner: User
) -> None:
    """Muting is about the mailbox. The platform still has to be able to show
    that it raised the matter."""
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.muted_kinds = [NotificationKind.OBLIGATION_DUE]
        preference.save()

        result = _raise(org, org_owner)
        rows = {row.channel: row.state for row in result.notification.deliveries.all()}

    assert rows[Channel.IN_APP] == DeliveryState.SENT
    assert rows[Channel.EMAIL] == DeliveryState.SUPPRESSED


def test_an_urgent_notification_ignores_a_mute(org: Tenant, org_owner: User) -> None:
    """A statutory deadline is not something the product offers to stop mentioning."""
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.muted_kinds = [NotificationKind.OBLIGATION_DUE]
        preference.save()

        result = _raise(org, org_owner, severity=Severity.URGENT)
        rows = {row.channel: row.state for row in result.notification.deliveries.all()}

    assert rows[Channel.EMAIL] == DeliveryState.PENDING


def test_whatsapp_is_off_until_asked_for(org: Tenant, org_owner: User) -> None:
    """It costs money per message. An opt-out default surprises people with a bill."""
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        result = _raise(org, org_owner)
        row = Delivery.objects.get(notification=result.notification, channel=Channel.WHATSAPP)

    assert row.state == DeliveryState.SUPPRESSED
    assert row.detail == "preference"


def test_whatsapp_without_an_approved_template_says_so(org: Tenant, org_owner: User) -> None:
    """Meta approves templates by name, and approval takes weeks.

    A kind with no template is recorded as suppressed *with the reason*, so that
    "why did nobody get a WhatsApp" is answerable without reading the source.
    """
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.whatsapp_enabled = True
        preference.save()

        # WORK_ASSIGNED has no approved template — it is internal, not client-facing.
        result = _raise(org, org_owner, kind=NotificationKind.WORK_ASSIGNED, dedupe_key="work:1")
        row = Delivery.objects.get(notification=result.notification, channel=Channel.WHATSAPP)

    assert row.state == DeliveryState.SUPPRESSED
    assert "template" in row.detail


def test_whatsapp_sends_when_enabled_and_approved(org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.whatsapp_enabled = True
        preference.save()

        result = _raise(org, org_owner, context={"entity": "Acme", "obligation": "GSTR-3B"})
        deliver_notification(
            tenant_id=str(org.pk),
            notification_id=str(result.notification.pk),
            context={"entity": "Acme", "obligation": "GSTR-3B", "date": "20 Sep 2026"},
        )
        row = Delivery.objects.get(notification=result.notification, channel=Channel.WHATSAPP)

    assert row.state == DeliveryState.SENT


def test_a_failed_email_does_not_stop_the_whatsapp(
    org: Tenant, org_owner: User, monkeypatch
) -> None:
    """The two channels exist so that one of them getting through is enough."""
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.whatsapp_enabled = True
        preference.save()

        result = _raise(org, org_owner)

        def explode(*args: object, **kwargs: object) -> None:
            raise OSError("smtp is down")

        monkeypatch.setattr("stacos.notifications.services.send_mail", explode)
        deliver_notification(tenant_id=str(org.pk), notification_id=str(result.notification.pk))

        rows = {row.channel: row.state for row in result.notification.deliveries.all()}

    assert rows[Channel.EMAIL] == DeliveryState.FAILED
    assert rows[Channel.WHATSAPP] == DeliveryState.SENT


def test_a_recipient_with_no_email_is_not_reachable_rather_than_failed(
    org: Tenant, org_owner: User
) -> None:
    """Not a failure, and never worth retrying."""
    org_owner.email = ""
    org_owner.save(update_fields=["email"])

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        result = _raise(org, org_owner)
        deliver_notification(tenant_id=str(org.pk), notification_id=str(result.notification.pk))
        row = Delivery.objects.get(notification=result.notification, channel=Channel.EMAIL)

    assert row.state == DeliveryState.NOT_REACHABLE


# ===========================================================================
# Digests
# ===========================================================================


def test_a_digest_subscriber_gets_no_immediate_email(org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.digest = DigestFrequency.DAILY
        preference.save()

        result = _raise(org, org_owner)
        channels = {row.channel for row in result.notification.deliveries.all()}

    # Only the in-app record. No pending email rows, because a queue that never
    # drains looks identical to a broken one.
    assert channels == {Channel.IN_APP}
    assert mail.outbox == []


def test_an_urgent_notification_defeats_the_digest(org: Tenant, org_owner: User) -> None:
    """The whole reason severity exists."""
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.digest = DigestFrequency.DAILY
        preference.save()

        result = _raise(org, org_owner, severity=Severity.URGENT)
        channels = {row.channel for row in result.notification.deliveries.all()}

    assert Channel.EMAIL in channels


def test_the_digest_collects_and_marks(org: Tenant, org_owner: User) -> None:
    from stacos.notifications.tasks import send_digest

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.digest = DigestFrequency.DAILY
        preference.save()

        _raise(org, org_owner, dedupe_key="a", title="GSTR-1 is due")
        _raise(org, org_owner, dedupe_key="b", title="TDS payment is due")

        result = send_digest(tenant_id=str(org.pk), user_id=str(org_owner.pk))

        assert result["status"] == "sent"
        assert result["items"] == 2
        digest = Notification.objects.get(kind=NotificationKind.DIGEST)
        assert "GSTR-1 is due" in digest.body
        # Marked, so tomorrow's digest does not repeat them.
        assert (
            Notification.objects.filter(digested_at__isnull=False, recipient=org_owner).count() == 2
        )


def test_an_empty_digest_is_not_sent(org: Tenant, org_owner: User) -> None:
    """ "Nothing to report" mail is the fastest way to teach somebody to ignore you."""
    from stacos.notifications.tasks import send_digest

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = preferences_for(tenant_id=org.pk, user=org_owner)
        preference.digest = DigestFrequency.DAILY
        preference.save()

        assert send_digest(tenant_id=str(org.pk), user_id=str(org_owner.pk))["status"] == "empty"


# ===========================================================================
# The ladder
# ===========================================================================


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [
        (10, None),
        (7, "T-7"),
        (5, None),
        (3, "T-3"),
        (2, None),
        (1, "T-1"),
        (0, "T-0"),
        (-1, None),
        (-3, "T+3"),
        (-4, None),
        (-6, "T+6"),
    ],
)
def test_the_reminder_ladder(days_left: int, expected: str | None) -> None:
    """Escalating, then every third day once overdue.

    A daily reminder from day thirty is noise by day five; a system that goes
    quiet once something is overdue has abandoned the user at the exact point
    the product is meant to earn its keep.
    """
    assert _rung(days_left) == expected


def test_the_sweep_notifies_about_an_upcoming_obligation(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    from stacos.engine.lifecycle import State
    from stacos.obligations.models import ObligationInstance

    day = timezone.localdate()

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        obligation = ObligationInstance.objects.create(
            tenant=org,
            entity=entity_a,
            definition_code="gst-gstr-3b-monthly",
            title="GSTR-3B",
            category="GST",
            period_key="2026-08",
            period_label="August 2026",
            due_date=day + timedelta(days=max(LADDER)),
            state=State.NOT_STARTED,
        )

    result = sweep_obligation_reminders(tenant_id=str(org.pk), as_of=day.isoformat())

    assert result["raised"] >= 1
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        raised = Notification.objects.filter(subject_id=obligation.pk)
        assert raised.exists()
        assert raised.first().dedupe_key.endswith(f"T-{max(LADDER)}")


def test_the_sweep_is_silent_on_a_quiet_day(org: Tenant, entity_a: Entity, org_owner: User) -> None:
    from stacos.engine.lifecycle import State
    from stacos.obligations.models import ObligationInstance

    day = timezone.localdate()

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        ObligationInstance.objects.create(
            tenant=org,
            entity=entity_a,
            definition_code="gst-gstr-3b-monthly",
            title="GSTR-3B",
            category="GST",
            period_key="2026-08",
            due_date=day + timedelta(days=5),
            state=State.NOT_STARTED,
        )

    assert sweep_obligation_reminders(tenant_id=str(org.pk), as_of=day.isoformat()) == {"raised": 0}


def test_a_filed_obligation_is_not_reminded_about(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """The trap in `live()`.

    It excludes superseded and archived rows — not finished ones. A reminder
    that GSTR-3B is due on the 20th, arriving after it was filed on the 18th, is
    the fastest way to teach a firm that these messages are noise and to stop
    reading the one that mattered.
    """
    from stacos.engine.lifecycle import State
    from stacos.obligations.models import ObligationInstance

    day = timezone.localdate()

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        ObligationInstance.objects.create(
            tenant=org,
            entity=entity_a,
            definition_code="gst-gstr-3b-monthly",
            title="GSTR-3B",
            category="GST",
            period_key="2026-08",
            due_date=day + timedelta(days=3),
            state=State.FILED,
            filed_on=day - timedelta(days=1),
            filing_reference="AA240800001234",
        )

    assert sweep_obligation_reminders(tenant_id=str(org.pk), as_of=day.isoformat()) == {"raised": 0}


def test_running_the_sweep_twice_sends_once(org: Tenant, entity_a: Entity, org_owner: User) -> None:
    """The sweep runs hourly. This is the assertion that makes that acceptable."""
    from stacos.engine.lifecycle import State
    from stacos.obligations.models import ObligationInstance

    day = timezone.localdate()

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        ObligationInstance.objects.create(
            tenant=org,
            entity=entity_a,
            definition_code="gst-gstr-3b-monthly",
            title="GSTR-3B",
            category="GST",
            period_key="2026-08",
            due_date=day + timedelta(days=3),
            state=State.NOT_STARTED,
        )

    first = sweep_obligation_reminders(tenant_id=str(org.pk), as_of=day.isoformat())
    second = sweep_obligation_reminders(tenant_id=str(org.pk), as_of=day.isoformat())

    assert first["raised"] >= 1
    assert second["raised"] == 0


# ===========================================================================
# Screens
# ===========================================================================


def test_the_list_shows_only_your_own(
    signed_in: Client, org: Tenant, org_owner: User, org_member: User
) -> None:
    """Not a permission question — somebody else's notification is a row that
    should never be in the queryset at all."""
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        _raise(org, org_owner, title="Yours")
        _raise(org, org_member, title="Theirs", dedupe_key="other")

    response = signed_in.get(reverse("notifications:list"))

    assert response.status_code == 200
    assert b"Yours" in response.content
    assert b"Theirs" not in response.content


def test_marking_read_clears_the_badge(signed_in: Client, org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        result = _raise(org, org_owner)

    response = signed_in.post(reverse("notifications:mark_read", args=[result.notification.pk]))

    assert response.status_code == 200
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        assert unread_count(user=org_owner) == 0


def test_marking_somebody_elses_notification_read_is_a_404(
    signed_in: Client, org: Tenant, org_member: User
) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        result = _raise(org, org_member, title="Theirs")

    response = signed_in.post(reverse("notifications:mark_read", args=[result.notification.pk]))

    assert response.status_code == 404


def test_mark_all_read(signed_in: Client, org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        _raise(org, org_owner, dedupe_key="a")
        _raise(org, org_owner, dedupe_key="b")

    response = signed_in.post(reverse("notifications:mark_all"))

    assert response.status_code == 200
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        assert unread_count(user=org_owner) == 0


def test_the_panel_renders(signed_in: Client, org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        _raise(org, org_owner)

    response = signed_in.get(reverse("notifications:panel"), HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert b"GSTR-3B" in response.content


def test_preferences_save(signed_in: Client, org: Tenant, org_owner: User) -> None:
    response = signed_in.post(
        reverse("notifications:preferences"),
        {
            "email_enabled": "on",
            "digest": DigestFrequency.DAILY,
            "digest_hour": "7",
            "muted_kinds": [NotificationKind.INVOICE_ISSUED],
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        preference = NotificationPreference.objects.get(user=org_owner)
    assert preference.digest == DigestFrequency.DAILY
    assert preference.muted_kinds == [NotificationKind.INVOICE_ISSUED]
    assert not preference.whatsapp_enabled


# ===========================================================================
# Isolation
# ===========================================================================


def test_a_notification_does_not_leak_across_tenants(
    org: Tenant, other_org: Tenant, org_owner: User
) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        _raise(org, org_owner)

    with tenant_context(tenant_ids={other_org.pk}, reason="test"):
        assert Notification.objects.count() == 0

    with platform_scope(reason="test"):
        assert Notification.objects.count() == 1
