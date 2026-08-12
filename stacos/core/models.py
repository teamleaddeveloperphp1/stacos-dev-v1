"""
Base models every STACOS app builds on.

``TenantScopedModel`` is the important one: inheriting it is what makes a table
participate in tenant isolation, and CI fails the build for any model that
neither inherits it nor appears in an explicit "this is global" allowlist. That
way nobody adds a model without *deciding* its tenancy.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

from django.conf import settings
from django.contrib.postgres.indexes import BrinIndex
from django.db import models
from django.utils import timezone

from stacos.core.exceptions import CrossTenantWriteError
from stacos.core.ids import uuid7
from stacos.core.managers import TenantScopedManager, UnscopedManager
from stacos.core.scope import current_scope

__all__ = [
    "AuditAction",
    "AuditLog",
    "SoftDeleteModel",
    "TaskRun",
    "TenantScopedModel",
    "TimeStampedModel",
    "UUIDModel",
]


class UUIDModel(models.Model):
    """Primary key is a time-ordered UUIDv7. See :mod:`stacos.core.ids`."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteModel(models.Model):
    """Soft deletion, because compliance data is evidence.

    Nothing a tenant has attached history to is ever removed by a `DELETE`; it is
    archived and stops appearing in the default queryset. The obligation
    materialiser depends on this — it must never destroy an instance that already
    carries evidence.
    """

    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    archive_reason = models.TextField(blank=True)

    class Meta:
        abstract = True

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def archive(self, *, reason: str = "") -> None:
        self.archived_at = timezone.now()
        self.archive_reason = reason
        # `updated_at` only exists when the concrete model also inherits
        # TimeStampedModel, which most but not all do. Naming it unconditionally
        # would raise on the ones that do not.
        fields = ["archived_at", "archive_reason"]
        if hasattr(self, "updated_at"):
            fields.append("updated_at")
        self.save(update_fields=fields)


class TenantScopedModel(UUIDModel, TimeStampedModel):
    """Base for every row a tenant owns.

    Subclasses get:

    * a ``tenant`` foreign key, always populated;
    * ``objects`` — a manager that raises unless an :class:`AccessScope` is
      bound, and filters to what that scope permits;
    * ``objects_unscoped`` — the audited escape hatch;
    * a write guard on ``save()``.

    Models that are *about* an entity (rather than merely owned by a tenant) set
    ``ENTITY_FIELD`` so engagement scoping applies. That distinction is
    load-bearing: a practice engaged for two of a group's eight subsidiaries must
    see obligations for those two entities and nothing else, while never seeing
    the client's user list at all.
    """

    #: Column the tenant filter is applied to. Overridable for odd schemas.
    TENANT_FIELD: ClassVar[str] = "tenant_id"

    #: Column the entity filter is applied to, or ``None`` for tenant-level rows.
    ENTITY_FIELD: ClassVar[str | None] = None

    #: Set ``False`` for system-written, append-only rows (audit entries) that
    #: must be recordable even when the actor's engagement is read-only.
    ENFORCE_WRITE_SCOPE: ClassVar[bool] = True

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )

    objects = TenantScopedManager()
    objects_unscoped = UnscopedManager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._assert_writable()
        super().save(*args, **kwargs)

    def _assert_writable(self) -> None:
        """Refuse to persist a row belonging to a tenant outside the write scope.

        Catches the subtler half of a scoping bug: reading is guarded by the
        manager, but a hand-constructed instance with an attacker-supplied
        ``tenant_id`` would otherwise write straight through it.
        """
        if not self.ENFORCE_WRITE_SCOPE:
            return
        scope = current_scope()
        if scope is None or scope.bypass:
            # No scope at all is caught on the read path; platform scope is
            # audited at the point it is entered.
            return
        tenant_id: UUID | None = getattr(self, "tenant_id", None)
        if tenant_id is None:
            return
        if not scope.can_write(tenant_id):
            raise CrossTenantWriteError(
                f"Refusing to write {self._meta.label} for tenant {tenant_id}, "
                f"which is outside the current write scope ({scope.reason})."
            )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class AuditAction(models.TextChoices):
    """Coarse verbs. The specific object and payload carry the detail."""

    CREATE = "CREATE", "Created"
    UPDATE = "UPDATE", "Updated"
    DELETE = "DELETE", "Deleted"
    ARCHIVE = "ARCHIVE", "Archived"
    VIEW = "VIEW", "Viewed"
    DOWNLOAD = "DOWNLOAD", "Downloaded"
    EXPORT = "EXPORT", "Exported"
    TRANSITION = "TRANSITION", "State changed"
    LOGIN = "LOGIN", "Signed in"
    LOGIN_FAILED = "LOGIN_FAILED", "Sign-in failed"
    LOGOUT = "LOGOUT", "Signed out"
    OTP_SENT = "OTP_SENT", "Verification code sent"
    OTP_VERIFIED = "OTP_VERIFIED", "Verification completed"
    OTP_FAILED = "OTP_FAILED", "Verification failed"
    STEP_UP = "STEP_UP", "Re-authenticated"
    PERMISSION_CHANGE = "PERMISSION_CHANGE", "Permissions changed"
    ENGAGEMENT_GRANT = "ENGAGEMENT_GRANT", "Access granted"
    ENGAGEMENT_REVOKE = "ENGAGEMENT_REVOKE", "Access revoked"
    PLATFORM_ACCESS = "PLATFORM_ACCESS", "Platform-wide access"
    IMPERSONATE = "IMPERSONATE", "Support impersonation"


class AuditLog(UUIDModel):
    """Append-only record of everything that mattered.

    This is a product feature, not plumbing: it is what a business shows a
    regulator, or an acquirer during due diligence. A migration revokes
    ``UPDATE`` and ``DELETE`` on this table from the application role and adds a
    trigger, so "append-only" is enforced by PostgreSQL rather than by good
    intentions.

    Not a :class:`TenantScopedModel` — ``tenant`` is nullable, because platform
    and pre-authentication events (a failed sign-in for an unknown email) have no
    tenant, and those rows must still be recorded.
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    entity = models.ForeignKey(
        "tenancy.Entity",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    #: Set when a platform user is acting on a tenant user's behalf. Support
    #: access is consent-gated, time-boxed and visible to the user; this column
    #: is how "who really did this" survives an impersonated session.
    acting_as = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries_impersonated",
    )
    actor_label = models.CharField(
        max_length=255,
        blank=True,
        help_text="Denormalised actor description, so the row stays readable after "
        "the user is deleted.",
    )

    action = models.CharField(max_length=32, choices=AuditAction.choices, db_index=True)
    object_type = models.CharField(max_length=100, blank=True, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    object_label = models.CharField(max_length=255, blank=True)

    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    context = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    scope_reason = models.CharField(max_length=120, blank=True)

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = models.Manager()

    class Meta:
        verbose_name = "audit entry"
        verbose_name_plural = "audit entries"
        ordering = ["-occurred_at"]
        indexes = [
            # The tenant timeline — the most common read by far.
            models.Index(fields=["tenant", "-occurred_at"], name="audit_tenant_time_idx"),
            # "What happened to this object", for the detail-page timeline.
            models.Index(
                fields=["object_type", "object_id", "-occurred_at"],
                name="audit_object_time_idx",
            ),
            # "What did this user do", for security review.
            models.Index(fields=["actor", "-occurred_at"], name="audit_actor_time_idx"),
            # BRIN over the append-only time axis: a fraction of the size of a
            # B-tree on a table that only ever grows in timestamp order.
            BrinIndex(fields=["occurred_at"], name="audit_occurred_brin_idx", pages_per_range=64),
        ]

    def __str__(self) -> str:
        return f"{self.occurred_at:%Y-%m-%d %H:%M} {self.action} {self.object_type}"


# ---------------------------------------------------------------------------
# Task idempotency
# ---------------------------------------------------------------------------


class TaskRun(models.Model):
    """At-most-once bookkeeping for Celery tasks.

    ``task_acks_late`` guarantees at-*least*-once delivery, so exactly-once only
    exists if the task is idempotent. The unique constraint on
    ``idempotency_key`` is how a task becomes idempotent: claim the key inside a
    transaction, and a redelivered message finds the row already there.

    This must exist before the first money-touching or filing-touching task
    ships, which is why it lands in the foundation rather than with billing.
    """

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    idempotency_key = models.CharField(max_length=200, unique=True)
    task_name = models.CharField(max_length=200, db_index=True)
    tenant_id_value = models.UUIDField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)
    result_ref = models.CharField(max_length=200, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        indexes = [models.Index(fields=["task_name", "-started_at"], name="taskrun_name_time_idx")]

    def __str__(self) -> str:
        return f"{self.task_name} [{self.status}]"
