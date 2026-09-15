"""
Demo data for one specific, already-registered account.

`seed_dev` builds a whole world from nothing, for a database nobody has signed
into yet. This is the opposite case: a real person — by default
``gauravrautedu@gmail.com`` — has already created an account through the
product's own sign-up flow and has no tenant yet, and needs one to try the
product end to end. That means:

* a business tenant with that account as its Owner / Director;
* an entity with a real enough profile that the engine actually produces a
  calendar, so there is something to look at;
* a practice, engaged on the new entity, so a firm's-eye view of the same
  calendar can be exercised too — its own tenant, owned by a Partner.

Unlike `seed_dev`, this command never touches the named account's password or
verification flags: that user is real, may already have a password nobody
running this command knows, and the only thing this command is allowed to do
to that row is attach it to a tenant it does not yet belong to.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.scope import platform_scope, tenant_context
from stacos.engagements.models import Engagement
from stacos.obligations.services import materialise
from stacos.tenancy.models import (
    ComplianceCategory,
    Entity,
    EntityProfile,
    EntityRegistration,
    Membership,
    Tenant,
)
from stacos.tenancy.services import provision_tenant

DEV_PASSWORD = "stacos-dev-password"  # noqa: S105 - development fixture only


class Command(BaseCommand):
    help = "Seed a business, a working calendar and an engaged practice for one real dev account."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--email",
            default="gauravrautedu@gmail.com",
            help="The already-registered account to build a business around.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        email: str = options["email"]
        owner = User.objects.filter(email__iexact=email).first()
        if owner is None:
            raise CommandError(
                f"No user with email {email!r}. This command attaches a tenant to an "
                f"account that has already signed up — it does not create one."
            )

        org = self._organisation_for(owner)
        entity = self._entity(org)

        practice = self._practice()
        self._engagement(practice, entity)

        as_of = timezone.localdate()
        with tenant_context(tenant_ids={org.id}, reason="seed_gaurav_demo:materialise"):
            run = materialise(entity, as_of=as_of, trigger="ONBOARDING", actor=owner)
        self.stdout.write(
            self.style.SUCCESS(f"  Calendar built for {entity.name}: {run.summary()}")
        )

        self._report(email, org, practice)

    # -- The business ---------------------------------------------------------

    def _organisation_for(self, owner: User) -> Tenant:
        """Reuse an existing business membership, or provision a fresh one.

        Idempotent on purpose: running this command twice must not create a
        second business for the same person.
        """
        existing = self._tenant_owned_by(owner, Tenant.Type.ORGANISATION)
        if existing is not None:
            self.stdout.write(f"  Found existing business: {existing.name}")
            return existing

        org = provision_tenant(
            "Gaurav Enterprises Pvt Ltd",
            owner=owner,
            country="IN",
            tenant_type=Tenant.Type.ORGANISATION,
            reason="seed_gaurav_demo",
        )
        self.stdout.write(f"  Organisation: {org.name} (owner: {owner.email})")
        return org

    def _entity(self, org: Tenant) -> Entity:
        with tenant_context(tenant_ids={org.id}, reason="seed_gaurav_demo:entity"):
            entity: Entity
            entity, created = Entity.objects.get_or_create(
                tenant=org,
                short_code="GEPL",
                defaults={
                    "name": "Gaurav Textiles Pvt Ltd",
                    "legal_name": "Gaurav Textiles Private Limited",
                    "entity_type": "PVT_LTD",
                    "country": "IN",
                    "incorporation_date": date(2016, 4, 1),
                    "registered_office_state": "IN-GJ",
                    "registered_office_address": "Ring Road, Surat, Gujarat 395002",
                },
            )
            EntityProfile.objects.get_or_create(
                entity=entity,
                defaults={
                    "tenant": org,
                    "aggregate_turnover": Decimal("320000000"),
                    "employee_count": 140,
                    "contractor_count": 30,
                    "paid_up_capital": Decimal("15000000"),
                    "nic_code": "13921",
                    "sector": "Manufacturing",
                    "sub_sector": "Textiles",
                    "states_of_operation": ["IN-GJ"],
                    "facts": {
                        "gst_scheme": "REGULAR",
                        "qrmp_opted": False,
                        "is_listed": False,
                        "has_boiler": True,
                        "has_msme_vendors": True,
                        "has_export_import": False,
                        "deals_in_hazardous_material": False,
                        "women_employees_count": 22,
                    },
                },
            )
            for reg_type, value, jurisdiction in [
                ("PAN", "AAACG4321K", ""),
                ("TAN", "SRTG54321E", ""),
                ("GST", "24AAACG4321K1Z1", "IN-GJ"),
                ("CIN", "U17110GJ2016PTC087654", ""),
            ]:
                EntityRegistration.objects.get_or_create(
                    entity=entity,
                    type=reg_type,
                    jurisdiction=jurisdiction,
                    defaults={"tenant": org, "value": value, "valid_from": date(2016, 4, 1)},
                )
            self.stdout.write(f"  {'Created' if created else 'Found'} entity: {entity.name}")
            return entity

    # -- The practice, engaged on the same entity ------------------------------

    def _practice(self) -> Tenant:
        partner = self._get_or_create_user(
            "anand.partner@gaurav-demo.example", "Anand Mehta", "+919900000201"
        )
        existing = self._tenant_owned_by(partner, Tenant.Type.PRACTICE)
        if existing is not None:
            self.stdout.write(f"  Found existing practice: {existing.name}")
            return existing

        practice = provision_tenant(
            "Mehta & Co, Chartered Accountants (demo)",
            owner=partner,
            country="IN",
            tenant_type=Tenant.Type.PRACTICE,
            reason="seed_gaurav_demo",
        )
        self.stdout.write(f"  Practice: {practice.name} (Partner: {partner.email})")
        return practice

    def _engagement(self, practice: Tenant, entity: Entity) -> None:
        with tenant_context(tenant_ids=entity.tenant_id, reason="seed_gaurav_demo:engagement"):
            Engagement.objects.get_or_create(
                practice_tenant=practice,
                entity=entity,
                defaults={
                    "tenant": entity.tenant,
                    "status": Engagement.Status.ACTIVE,
                    "initiated_by_side": Engagement.Side.ORGANISATION,
                    "categories": [
                        ComplianceCategory.TAX_INDIRECT,
                        ComplianceCategory.TAX_DIRECT,
                        ComplianceCategory.CORPORATE_SECRETARIAL,
                    ],
                    "permissions": [
                        "tenancy.entity.view",
                        "tenancy.profile.view",
                        "tenancy.registration.view",
                        "core.search",
                    ],
                    "starts_on": timezone.localdate(),
                    "engagement_letter_ref": "GD/2026/GEPL/01",
                },
            )
        self.stdout.write(f"  Engagement: {practice.name} on {entity.name} (tax and secretarial)")

    # -- Shared helpers -----------------------------------------------------

    def _tenant_owned_by(self, user: User, tenant_type: str) -> Tenant | None:
        """Idempotency check: has this person already got one of these?

        Looked up by membership rather than by a guessed slug — ``unique_slug``
        derives the slug from the tenant's display name, so re-deriving it here
        would silently stop matching the moment either name changed, and this
        command would quietly provision a second tenant on every re-run instead
        of reusing the first.
        """
        with platform_scope(reason="seed_gaurav_demo:lookup"):
            membership = (
                Membership.objects.select_related("tenant")
                .filter(user=user, status=Membership.Status.ACTIVE, tenant__type=tenant_type)
                .first()
            )
        return membership.tenant if membership is not None else None

    def _get_or_create_user(self, email: str, name: str, phone: str) -> User:
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "full_name": name,
                "phone_e164": phone,
                "email_verified": True,
                "phone_verified": True,
            },
        )
        if created:
            user.set_password(DEV_PASSWORD)
            user.save()
        return user

    def _report(self, email: str, org: Tenant, practice: Tenant) -> None:
        self.stdout.write(self.style.SUCCESS("\nDemo data ready."))
        self.stdout.write(
            f"""
  Sign in as yourself: {email}
    Owner / Director of {org.name} — open the compliance calendar.

  A practice engaged on your entity, for the accountant's-eye view
  ({DEV_PASSWORD}):
    Owned by its Partner, {practice.name}

  With WHATSAPP_PROVIDER=console the verification codes are printed to this terminal.
"""
        )
