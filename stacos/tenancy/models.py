"""
Tenants, entities, and who may do what inside them.

The shape that matters:

* A **Tenant** is a customer account — either an ``ORGANISATION`` (a business), a
  ``PRACTICE`` (a CA/CS firm), or a ``DEALER`` (a channel partner). One product,
  three kinds of account, one set of machinery.
* An **Entity** is a legal person inside an organisation tenant. A group with six
  subsidiaries and two LLPs is *one* tenant and *eight* entities. Compliance
  attaches to entities, never to tenants — which is why almost everything
  downstream is entity-scoped rather than tenant-scoped.
* A **Membership** links a global :class:`~stacos.accounts.models.User` to a
  tenant with a role and a set of scoping dimensions.

Cross-tenant access never appears here. It exists only through an
:class:`~stacos.engagements.models.Engagement`, so there is exactly one place to
audit when asking "how can this practice see this client's data".
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.ids import uuid7
from stacos.core.models import SoftDeleteModel, TenantScopedModel, TimeStampedModel
from stacos.jurisdictions.facts import REGISTRY
from stacos.jurisdictions.validators import validate_registration_value

__all__ = [
    "ComplianceCategory",
    "Entity",
    "EntityFactValue",
    "EntityPremises",
    "EntityProfile",
    "EntityRegistration",
    "Membership",
    "Role",
    "Tenant",
    "TenantInvitation",
]


class ComplianceCategory(models.TextChoices):
    """The axis an engagement is scoped along, and a department user is limited to."""

    TAX_INDIRECT = "TAX_INDIRECT", _("Indirect tax")
    TAX_DIRECT = "TAX_DIRECT", _("Direct tax")
    CORPORATE_SECRETARIAL = "CORPORATE_SECRETARIAL", _("Corporate secretarial")
    LABOUR = "LABOUR", _("Labour and employment")
    ENVIRONMENT = "ENVIRONMENT", _("Environment")
    SAFETY_FIRE = "SAFETY_FIRE", _("Safety and fire")
    LICENSING = "LICENSING", _("Licensing")
    FEMA_RBI = "FEMA_RBI", _("Foreign exchange and RBI")
    SECTORAL = "SECTORAL", _("Sector-specific")
    DATA_PRIVACY = "DATA_PRIVACY", _("Data privacy")
    INTERNAL_GOVERNANCE = "INTERNAL_GOVERNANCE", _("Internal governance")


class Tenant(TimeStampedModel):
    """A customer account. Not itself tenant-scoped — it *is* the tenant."""

    class Type(models.TextChoices):
        ORGANISATION = "ORGANISATION", _("Business")
        PRACTICE = "PRACTICE", _("Professional firm")
        DEALER = "DEALER", _("Channel partner")

    class Status(models.TextChoices):
        TRIAL = "TRIAL", _("Trial")
        ACTIVE = "ACTIVE", _("Active")
        PAST_DUE = "PAST_DUE", _("Payment overdue")
        SUSPENDED = "SUSPENDED", _("Suspended")
        CLOSED = "CLOSED", _("Closed")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    type = models.CharField(max_length=16, choices=Type.choices, db_index=True)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TRIAL)

    country = models.CharField(max_length=2, default="IN")
    jurisdiction_pack = models.ForeignKey(
        "jurisdictions.JurisdictionPack",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tenants",
    )

    #: Channel partner that sold and manages this account. A dealer relationship
    #: on its own grants **no** access to compliance data — that requires an
    #: explicit, time-boxed, client-approved grant.
    managed_by_dealer = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_tenants",
        limit_choices_to={"type": Type.DEALER},
    )
    #: Master distributor above this dealer, for commission splits.
    parent_dealer = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="sub_dealers"
    )

    settings = models.JSONField(default=dict, blank=True)
    logo_url = models.URLField(blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["type", "status"], name="tenant_type_status_idx"),
            models.Index(fields=["managed_by_dealer"], name="tenant_dealer_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_organisation(self) -> bool:
        return self.type == self.Type.ORGANISATION

    @property
    def is_practice(self) -> bool:
        return self.type == self.Type.PRACTICE

    @property
    def has_app_access(self) -> bool:
        """Suspended tenants become read-only. Data is never deleted for non-payment."""
        return self.status in {self.Status.TRIAL, self.Status.ACTIVE, self.Status.PAST_DUE}


class Role(TimeStampedModel):
    """A named bundle of permission codes.

    System roles (``tenant`` null) ship with the product. Tenant roles are
    customer-defined and gated behind a plan. Permissions are stored as codes, so
    a role survives the registry growing new capabilities.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name="roles"
    )
    code = models.SlugField(max_length=60)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    tenant_type = models.CharField(max_length=16, choices=Tenant.Type.choices)
    permissions = models.JSONField(default=list, blank=True)
    is_system = models.BooleanField(default=False)
    rank = models.PositiveSmallIntegerField(
        default=100, help_text="Lower is more senior. Used for ordering, not for authority."
    )

    objects = models.Manager()

    class Meta:
        ordering = ["tenant_type", "rank", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["code", "tenant_type"],
                condition=models.Q(tenant__isnull=True),
                name="role_system_code_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "code"],
                condition=models.Q(tenant__isnull=False),
                name="role_tenant_code_uniq",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Entity(TenantScopedModel, SoftDeleteModel):
    """A legal person. Compliance attaches here, not to the tenant."""

    # Engagements grant access to named entities, so the entity filter on this
    # model is its own primary key.
    ENTITY_FIELD: ClassVar[str | None] = "id"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        DORMANT = "DORMANT", _("Dormant")
        STRUCK_OFF = "STRUCK_OFF", _("Struck off")
        CLOSED = "CLOSED", _("Closed")

    class SetupStep(models.TextChoices):
        """How far the guided first-run setup has got for this entity.

        The step keys match the url-name suffixes in
        :mod:`stacos.tenancy.entity_setup`, because they name the same four
        screens and two spellings of one list is one spelling too many.
        """

        REGISTRATIONS = "registrations", _("Registrations")
        ANSWERS = "answers", _("Answer what applies")
        PACKS = "packs", _("Optional add-ons")
        BUILD = "build", _("Review and create")

    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=250, blank=True)
    short_code = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)

    entity_type = models.CharField(max_length=24)
    country = models.CharField(max_length=2, default="IN")
    incorporation_date = models.DateField(null=True, blank=True)
    cessation_date = models.DateField(null=True, blank=True)

    registered_office_state = models.CharField(max_length=12, blank=True)
    registered_office_address = models.TextField(blank=True)

    #: Group structure inside one tenant — a holding company and its subsidiaries.
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="subsidiaries"
    )

    #: The furthest step of the guided setup this entity has reached.
    #:
    #: Persisted rather than derived, because the thing being remembered is a
    #: *navigation* fact — "you were on the questions screen" — and nothing in
    #: the data model records it. Without this, closing the browser halfway
    #: through setup and coming back put the user at step one again, with no
    #: sign that anything they had already done had been kept. It had been; the
    #: product just never said so.
    #:
    #: Not a completion flag. Setup is finished when a ``MaterialisationRun``
    #: exists for the entity — which is what
    #: ``tenancy.views.entity_detail`` and ``obligations`` have always checked,
    #: and what keeps every entity that predates this column (default
    #: ``registrations``, calendar long since built) out of the resume path.
    setup_step = models.CharField(
        max_length=16,
        choices=SetupStep.choices,
        default=SetupStep.REGISTRATIONS,
        help_text="Furthest step reached in the guided first-run setup.",
    )

    class Meta:
        verbose_name_plural = "entities"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "short_code"],
                condition=models.Q(archived_at__isnull=True) & ~models.Q(short_code=""),
                name="entity_tenant_shortcode_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(cessation_date__isnull=True)
                | models.Q(incorporation_date__isnull=True)
                | models.Q(cessation_date__gte=models.F("incorporation_date")),
                name="entity_cessation_after_incorporation",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"], name="entity_tenant_status_idx"),
            models.Index(fields=["tenant", "name"], name="entity_tenant_name_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        # Enforced here rather than as a CheckConstraint because it spans tables.
        # An entity under a practice or dealer tenant would be meaningless: those
        # tenants hold engagements, not compliance obligations of their own.
        if self.tenant_id and self.tenant.type != Tenant.Type.ORGANISATION:
            raise ValidationError({"tenant": "Entities may only belong to an organisation tenant."})


class EntityProfile(TenantScopedModel):
    """The current answer to every fact the compliance engine asks about.

    Storage is deliberately hybrid. The dozen facts that are queried, aggregated
    or billed on get typed columns; the long tail of flags lives in ``facts``
    JSONB with a GIN index. The reason is not laziness: the flag set grows every
    time a jurisdiction or sector is added, and adding a flag must not require a
    migration. Validation against
    :data:`~stacos.jurisdictions.facts.REGISTRY` is what keeps that blob from
    rotting into three spellings of the same key.

    This row holds *current* values. History lives in
    :class:`EntityFactValue`, because applicability depends on what was true for
    a period, not on what is true today.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.OneToOneField(Entity, on_delete=models.CASCADE, related_name="profile")

    # -- Typed: queried, aggregated, or billed on ---------------------------
    aggregate_turnover = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    employee_count = models.PositiveIntegerField(null=True, blank=True)
    contractor_count = models.PositiveIntegerField(null=True, blank=True)
    paid_up_capital = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    net_worth = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    nic_code = models.CharField(max_length=12, blank=True)
    sector = models.CharField(max_length=100, blank=True)
    sub_sector = models.CharField(max_length=100, blank=True)

    states_of_operation = ArrayField(models.CharField(max_length=12), default=list, blank=True)

    # -- The long tail ------------------------------------------------------
    facts = models.JSONField(default=dict, blank=True)

    #: Bumped whenever anything here changes, so a materialisation plan can be
    #: checked for staleness before it is applied.
    version = models.PositiveIntegerField(default=1)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            GinIndex(fields=["facts"], name="entityprofile_facts_gin"),
            GinIndex(fields=["states_of_operation"], name="entityprofile_states_gin"),
            models.Index(fields=["tenant", "entity"], name="entityprofile_tenant_idx"),
        ]

    def __str__(self) -> str:
        return f"Profile of {self.entity}"

    def clean(self) -> None:
        super().clean()
        problems = REGISTRY.validate_facts(self.facts, strict=False)
        if problems:
            raise ValidationError({"facts": problems})

    def as_fact_dict(self) -> dict[str, Any]:
        """Flatten typed columns and JSONB into the single dict the engine reads."""
        return {
            "country": self.entity.country,
            "entity_type": self.entity.entity_type,
            "incorporation_date": (
                self.entity.incorporation_date.isoformat()
                if self.entity.incorporation_date
                else None
            ),
            "registered_office_state": self.entity.registered_office_state,
            "states_of_operation": list(self.states_of_operation),
            "aggregate_turnover": self.aggregate_turnover,
            "employee_count": self.employee_count,
            "contractor_count": self.contractor_count,
            "paid_up_capital": self.paid_up_capital,
            "net_worth": self.net_worth,
            "nic_code": self.nic_code,
            "sector": self.sector,
            "sub_sector": self.sub_sector,
            **self.facts,
        }


class EntityFactValue(TenantScopedModel):
    """Append-only history for facts that change and get restated.

    This exists because of a real failure mode. Turnover for FY 2025-26 is not
    known until books close in mid-2026, and it is frequently revised afterwards.
    A single mutable profile cannot answer "what was true for the July 2026
    period" — so the engine would confidently tell a client they had no
    obligation that they in fact had, which is the one mistake this product
    cannot make.

    ``recorded_at`` and ``valid_from``/``valid_to`` are different axes:
    *when we learned it* versus *when it was true*. Both are needed to explain a
    calendar retrospectively.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Source(models.TextChoices):
        USER = "USER", _("Entered by a user")
        IMPORT = "IMPORT", _("Imported from books")
        PORTAL = "PORTAL", _("Fetched from a government portal")
        DERIVED = "DERIVED", _("Computed")

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="fact_values")
    key = models.CharField(max_length=64, db_index=True)
    value = models.JSONField()

    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.USER)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    note = models.CharField(max_length=250, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["entity", "key", "-valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gte=models.F("valid_from")),
                name="factvalue_window_sane",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "key", "-valid_from"], name="factvalue_lookup_idx"),
            models.Index(fields=["tenant", "key"], name="factvalue_tenant_key_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.entity} {self.key}={self.value} from {self.valid_from}"


class EntityRegistration(TenantScopedModel, SoftDeleteModel):
    """A tax or statutory identifier the entity holds.

    Polymorphic on purpose: ``type`` names the identifier and
    :mod:`stacos.jurisdictions.validators` supplies the check. No core table
    carries a ``pan`` or ``gstin`` column, so adding the UAE is a data change.

    Individually addressable and validity-windowed because obligations fan out
    per registration: an entity with GST registrations in six states files six
    monthly returns, not one. A surrendered registration must stop generating
    obligations from the date it lapsed.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="registrations")
    type = models.CharField(max_length=24, db_index=True)
    value = models.CharField(max_length=64)
    jurisdiction = models.CharField(
        max_length=12, blank=True, help_text="Sub-jurisdiction code, e.g. IN-GJ for a state GSTIN."
    )
    issuing_authority = models.ForeignKey(
        "jurisdictions.Authority", on_delete=models.SET_NULL, null=True, blank=True
    )

    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)
    label = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["entity", "type", "jurisdiction"]
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "type", "jurisdiction"],
                condition=models.Q(archived_at__isnull=True) & models.Q(valid_to__isnull=True),
                name="entityreg_active_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_from__isnull=True)
                | models.Q(valid_to__gte=models.F("valid_from")),
                name="entityreg_window_sane",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "type"], name="entityreg_tenant_type_idx"),
            # Duplicate detection across tenants: two organisations onboarding the
            # same GSTIN is the "one legal person, two tenants" problem, and it
            # needs to be visible before it becomes an entity merge.
            models.Index(fields=["type", "value"], name="entityreg_type_value_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.value}"

    def clean(self) -> None:
        super().clean()
        if self.value:
            self.value = self.value.upper().strip()
            validate_registration_value(self.type, self.value)

    @property
    def is_active(self) -> bool:
        today = timezone.localdate()
        if self.archived_at is not None:
            return False
        if self.valid_from and today < self.valid_from:
            return False
        return not (self.valid_to and today > self.valid_to)


class EntityPremises(TenantScopedModel, SoftDeleteModel):
    """A physical location the entity operates from.

    Modelled as a first-class row because a whole family of obligations is *per
    premises*, not per entity: factory licence renewals, fire NOCs, pollution
    control consents, lift and boiler inspections, safety drills. An entity with
    three plants in two states has three of each.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="premises")
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=24)
    jurisdiction = models.CharField(max_length=12, blank=True)
    address = models.TextField(blank=True)

    operational_from = models.DateField(null=True, blank=True)
    operational_to = models.DateField(null=True, blank=True)

    #: Premises-level facts (has_boiler, worker headcount at this site) that
    #: differ from the entity as a whole.
    facts = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "entity premises"
        ordering = ["entity", "name"]
        indexes = [
            models.Index(fields=["tenant", "type"], name="premises_tenant_type_idx"),
            GinIndex(fields=["facts"], name="premises_facts_gin"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.type})"


class Membership(TenantScopedModel):
    """A user's place in a tenant, and the limits on what they can reach.

    The scoping dimensions are the point. A plant HR user gets ``LABOUR`` and
    ``SAFETY_FIRE`` on one entity and sees nothing financial anywhere; a practice
    manager gets a client set. Without these, "role" degenerates into a single
    seniority axis and every customer immediately asks for something it cannot
    express.
    """

    class Status(models.TextChoices):
        INVITED = "INVITED", _("Invited")
        ACTIVE = "ACTIVE", _("Active")
        SUSPENDED = "SUSPENDED", _("Suspended")
        REMOVED = "REMOVED", _("Removed")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INVITED)

    # -- Scoping dimensions -------------------------------------------------
    #: When true the member reaches every entity in the tenant, including ones
    #: created later. Otherwise ``entities`` is the exhaustive list.
    all_entities = models.BooleanField(default=True)
    entities = models.ManyToManyField(Entity, blank=True, related_name="memberships")

    #: Empty means every category. Otherwise the member is limited to these.
    categories = ArrayField(
        models.CharField(max_length=32, choices=ComplianceCategory.choices),
        default=list,
        blank=True,
    )
    department = models.CharField(max_length=60, blank=True)

    #: Practice-side only: the client tenants this member handles. Empty means
    #: the whole client book.
    client_tenants = models.ManyToManyField(Tenant, blank=True, related_name="practice_members")

    #: Extra permissions on top of the role, for one-off grants that do not
    #: justify a custom role.
    extra_permissions = models.JSONField(default=list, blank=True)

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships_invited",
    )
    joined_at = models.DateTimeField(null=True, blank=True)
    last_active_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="membership_tenant_user_uniq"),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="membership_user_status_idx"),
            models.Index(fields=["tenant", "status"], name="membership_tenant_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.tenant} as {self.role}"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def resolved_permissions(self) -> frozenset[str]:
        """Role permissions plus any one-off grants, closed over ``implies``."""
        from stacos.core.permissions import permission_registry

        return permission_registry.expand([*self.role.permissions, *self.extra_permissions])


class TenantInvitation(TenantScopedModel):
    """An invitation to join *this* organisation as a colleague.

    Distinct from ``engagements.EngagementInvitation``, which is a firm and a
    client agreeing to work together across two tenants. This is the far more
    ordinary thing: somebody at a business asking a colleague to join the
    workspace they already own.

    ``Membership`` has carried ``Status.INVITED`` and ``invited_by`` since the
    beginning, and it was never reachable — nothing in the product created one.
    It could not: a membership needs a ``user``, and the colleague being invited
    frequently has no account yet. The invitation holds an email until there is
    somebody to attach it to, and the membership is created at acceptance.

    Tenant-scoped, unlike the two cross-tenant invitations: the *inviting* tenant
    exists by definition and owns this row, which is what lets an administrator
    see and revoke their own outstanding invitations through the ordinary scoped
    manager.

    The raw token lives only in the emailed link — the same treatment as a
    trusted-device secret and a responder link.
    """

    ENTITY_FIELD: ClassVar[str | None] = None

    class Status(models.TextChoices):
        SENT = "SENT", _("Sent")
        ACCEPTED = "ACCEPTED", _("Accepted")
        REVOKED = "REVOKED", _("Revoked")

    email = models.EmailField()
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="invitations")
    message = models.TextField(blank=True)

    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SENT)
    expires_at = models.DateTimeField()

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenant_invitations_sent",
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenant_invitations_accepted",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One open invitation per address per tenant. Sending a second while
            # the first is live is how two links end up in one inbox and the
            # wrong one gets clicked.
            models.UniqueConstraint(
                fields=["tenant", "email"],
                condition=models.Q(status="SENT"),
                name="tenantinvitation_open_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"], name="tenantinvite_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Invitation for {self.email}"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.SENT and timezone.now() < self.expires_at
