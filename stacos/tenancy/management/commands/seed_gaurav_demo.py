"""
Demo data for one specific, already-registered account.

`seed_dev` builds a whole world from nothing, for a database nobody has signed
into yet. This is the opposite case: a real person — by default
``gauravrautedu@gmail.com`` — has already created an account through the
product's own sign-up flow and has no tenant yet, and needs one to try the
"assign an obligation to someone" flow end to end. That means:

* a business tenant with that account as its Owner / Director;
* an entity with a real enough profile that the engine actually produces a
  calendar, so there is something to assign;
* one colleague per business-side role the product documents
  (Compliance Manager, Department User, Viewer) so the calendar's "Owner"
  picker has real choices in it, not just the signed-in user;
* one demo account for every role this product ships that had **no** test
  account anywhere yet — the practice's Manager, and both dealer roles —
  plus a Partner and Staff for the same practice, engaged on the new entity,
  so a firm's-eye view of the same calendar can be exercised too.

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
from stacos.obligations.models import ObligationInstance
from stacos.obligations.services import materialise
from stacos.obligations.transitions import assign as assign_obligation
from stacos.tenancy.models import (
    ComplianceCategory,
    Entity,
    EntityProfile,
    EntityRegistration,
    Membership,
    Role,
    Tenant,
)
from stacos.tenancy.services import provision_tenant

DEV_PASSWORD = "stacos-dev-password"  # noqa: S105 - development fixture only


class Command(BaseCommand):
    help = (
        "Seed a business, colleagues and a working calendar for one real "
        "dev account, plus demo accounts for the roles this product "
        "documents but had no test account for yet."
    )

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
        colleagues = self._colleagues(org)

        practice = self._practice()
        practice_people = self._practice_people(practice)
        self._engagement(practice, entity)

        dealer = self._dealer()
        dealer_people = self._dealer_people(dealer)

        as_of = timezone.localdate()
        with tenant_context(tenant_ids={org.id}, reason="seed_gaurav_demo:materialise"):
            run = materialise(entity, as_of=as_of, trigger="ONBOARDING", actor=owner)
        self.stdout.write(
            self.style.SUCCESS(f"  Calendar built for {entity.name}: {run.summary()}")
        )

        self._assign_a_few(entity, colleagues, as_of=as_of, actor=owner)

        self._report(email, org, colleagues, practice, practice_people, dealer, dealer_people)

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

    def _colleagues(self, org: Tenant) -> dict[str, User]:
        """One person per business-side role — the calendar's 'Assign' picker.

        ``org-viewer`` had no demo account anywhere in this product yet, per
        the roles guide, so it is included here rather than left for later.
        """
        people = [
            (
                "org-compliance-manager",
                "ramesh.compliance@gaurav-team.example",
                "Ramesh Patel",
                "+919900000101",
            ),
            (
                "org-department-user",
                "hitesh.plant@gaurav-team.example",
                "Hitesh Shah",
                "+919900000102",
            ),
            ("org-viewer", "vidya.viewer@gaurav-team.example", "Vidya Rao", "+919900000103"),
        ]
        colleagues: dict[str, User] = {}
        with tenant_context(tenant_ids={org.id}, reason="seed_gaurav_demo:colleagues"):
            for role_code, email, name, phone in people:
                colleagues[role_code] = self._member(org, role_code, email, name, phone)
        return colleagues

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

    def _practice_people(self, practice: Tenant) -> dict[str, User]:
        """Manager had no demo account anywhere in this product yet."""
        people = [
            (
                "practice-manager",
                "priya.manager@gaurav-demo.example",
                "Priya Shah",
                "+919900000202",
            ),
            (
                "practice-staff",
                "nikhil.staff@gaurav-demo.example",
                "Nikhil Rao",
                "+919900000203",
            ),
        ]
        people_by_role: dict[str, User] = {}
        with tenant_context(tenant_ids={practice.id}, reason="seed_gaurav_demo:practice"):
            for role_code, email, name, phone in people:
                people_by_role[role_code] = self._member(practice, role_code, email, name, phone)
        return people_by_role

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

    # -- The dealer -------------------------------------------------------------

    def _dealer(self) -> Tenant:
        principal = self._get_or_create_user(
            "deepak.principal@gaurav-demo.example", "Deepak Nair", "+919900000301"
        )
        existing = self._tenant_owned_by(principal, Tenant.Type.DEALER)
        if existing is not None:
            self.stdout.write(f"  Found existing dealer: {existing.name}")
            return existing

        dealer = provision_tenant(
            "Channel Partners (demo)",
            owner=principal,
            country="IN",
            tenant_type=Tenant.Type.DEALER,
            reason="seed_gaurav_demo",
        )
        self.stdout.write(f"  Dealer: {dealer.name} (Principal: {principal.email})")
        return dealer

    def _dealer_people(self, dealer: Tenant) -> dict[str, User]:
        """Neither dealer role had a demo account anywhere in this product yet."""
        with tenant_context(tenant_ids={dealer.id}, reason="seed_gaurav_demo:dealer"):
            staff = self._member(
                dealer,
                "dealer-staff",
                "suresh.staff@gaurav-demo.example",
                "Suresh Kumar",
                "+919900000302",
            )
        return {"dealer-staff": staff}

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

    def _member(self, tenant: Tenant, role_code: str, email: str, name: str, phone: str) -> User:
        role = Role.objects.filter(
            tenant__isnull=True, code=role_code, tenant_type=tenant.type
        ).first()
        if role is None:
            raise CommandError(
                f"Missing system role {role_code!r} for {tenant.type}. "
                f"Run manage.py sync_system_roles first."
            )

        user = self._get_or_create_user(email, name, phone)
        Membership.objects.get_or_create(
            tenant=tenant,
            user=user,
            defaults={"role": role, "status": Membership.Status.ACTIVE},
        )
        self.stdout.write(f"    {email:<38} {role.name}")
        return user

    def _assign_a_few(
        self,
        entity: Entity,
        colleagues: dict[str, User],
        *,
        as_of: date,
        actor: User,
    ) -> None:
        """Hand a couple of freshly materialised obligations to colleagues.

        So the first thing the owner sees when they open the calendar is the
        feature already working, not an empty "Assign" link on every row.
        """
        manager = colleagues.get("org-compliance-manager")
        department_user = colleagues.get("org-department-user")
        if manager is None and department_user is None:
            return

        with tenant_context(tenant_ids={entity.tenant_id}, reason="seed_gaurav_demo:assign"):
            open_rows = list(
                ObligationInstance.objects.filter(entity=entity, archived_at__isnull=True).order_by(
                    "due_date"
                )[:6]
            )
            for index, obligation in enumerate(open_rows):
                assignee = manager if index % 2 == 0 else department_user
                if assignee is None:
                    continue
                assign_obligation(obligation, assignee=assignee, actor=actor)
        self.stdout.write(f"  Pre-assigned {min(len(open_rows), 6)} obligations to colleagues.")

    def _report(
        self,
        email: str,
        org: Tenant,
        colleagues: dict[str, User],
        practice: Tenant,
        practice_people: dict[str, User],
        dealer: Tenant,
        dealer_people: dict[str, User],
    ) -> None:
        self.stdout.write(self.style.SUCCESS("\nDemo data ready."))
        self.stdout.write(
            f"""
  Sign in as yourself: {email}
    Owner / Director of {org.name} — open the compliance calendar and the
    "Owner" column on any row (or "Assigned to" on the detail page) now opens
    an Assign picker listing your team below.

  Your team ({DEV_PASSWORD} for all of them):
    {colleagues["org-compliance-manager"].email:<38} Compliance Manager
    {colleagues["org-department-user"].email:<38} Department User (Plant HR, no financials)
    {colleagues["org-viewer"].email:<38} Viewer (read-only — new demo account)

  A practice engaged on your entity, for the accountant's-eye view:
    {practice_people["practice-manager"].email:<38} Manager, {practice.name} (new demo account)
    {practice_people["practice-staff"].email:<38} Staff / Article, {practice.name}

  A channel partner, for completeness (never sees your compliance data):
    {dealer_people["dealer-staff"].email:<38} Dealer Staff, {dealer.name} (new demo account)

  With WHATSAPP_PROVIDER=console the verification codes are printed to this terminal.
"""
        )
