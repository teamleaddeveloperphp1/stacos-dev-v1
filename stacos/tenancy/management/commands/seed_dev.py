"""
Demo data for development.

Builds a small but *realistic* world, because a seed that is too tidy hides the
problems the product actually has to solve. Specifically it creates:

* a Gujarat textile manufacturer with **two GST registrations and two factories**,
  so per-registration and per-premises fan-out is visible from the first day;
* a Bengaluru software exporter, to contrast a services entity with a factory;
* a CA firm engaged on **one** of the group's two entities and limited to tax
  categories, so scope narrowing is demonstrably working rather than assumed.

Every object is created inside an explicit tenant scope — the same discipline
production code follows, and a useful check that the scoping machinery works
outside a request.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from stacos.accounts.models import User
from stacos.core.scope import platform_scope, tenant_context
from stacos.engagements.models import Engagement
from stacos.jurisdictions.models import JurisdictionPack
from stacos.tenancy.models import (
    ComplianceCategory,
    Entity,
    EntityPremises,
    EntityProfile,
    EntityRegistration,
    Membership,
    Role,
    Tenant,
)

PASSWORD = "stacos-dev-password"  # noqa: S105 - development fixture only


class Command(BaseCommand):
    help = "Create demo tenants, entities, users and engagements for development."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--reset", action="store_true", help="Delete existing demo data first.")
        parser.add_argument(
            "--bare",
            action="store_true",
            help="Tenants and entities only — skip the in-flight work `seed_work` adds.",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        with platform_scope(reason="seed_dev"):
            if options["reset"]:
                self._reset()
            pack = self._jurisdiction_pack()
            org = self._organisation(pack)
            practice = self._practice(pack)
            self._users(org, practice)
            entities = self._entities(org)
            self._engagement(practice, entities["textile"])

        # The world exists; now fill it with work. Kept in its own command
        # because it imports nine modules, and a command in `tenancy` reaching
        # into `billing` and `practice` inverts the dependency direction the app
        # list is careful about. `--bare` skips it for a clean slate.
        if not options["bare"]:
            call_command("seed_work")

        self.stdout.write(self.style.SUCCESS("\nDemo data ready."))
        self.stdout.write(
            f"""
  Sign in at http://localhost:8000/auth/login/

    priya@vaibhav-textiles.example    {PASSWORD}   Owner, Vaibhav Textiles
    ramesh@vaibhav-textiles.example   {PASSWORD}   Compliance Manager
    hitesh@vaibhav-textiles.example   {PASSWORD}   Plant HR (Surat only, no financials)
    anand@sharma-associates.example   {PASSWORD}   Partner, Sharma & Associates
    nikhil@sharma-associates.example  {PASSWORD}   Staff

  With WHATSAPP_PROVIDER=console the verification codes are printed to this terminal.
"""
        )

    # -- Steps ---------------------------------------------------------------

    def _reset(self) -> None:
        Tenant.objects.filter(slug__in=["vaibhav-textiles", "sharma-associates"]).delete()
        User.objects.filter(email__endswith=".example").delete()
        self.stdout.write("  Removed existing demo data.")

    def _jurisdiction_pack(self) -> JurisdictionPack:
        pack, created = JurisdictionPack.objects.get_or_create(
            country="IN",
            defaults={
                "name": "India",
                "currency": "INR",
                "currency_symbol": "₹",
                "default_timezone": "Asia/Kolkata",
                "default_locale": "en-in",
                "digit_grouping": JurisdictionPack.DigitGrouping.INDIAN,
                # April to March. Read from here, never assumed in code.
                "fy_start_month": 4,
                "fy_start_day": 1,
                "fy_label_template": "FY {start_year}-{end_year_short}",
                "is_published": True,
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Found'} jurisdiction pack: India")
        return pack

    def _organisation(self, pack: JurisdictionPack) -> Tenant:
        tenant, _ = Tenant.objects.get_or_create(
            slug="vaibhav-textiles",
            defaults={
                "type": Tenant.Type.ORGANISATION,
                "name": "Vaibhav Textiles Group",
                "status": Tenant.Status.ACTIVE,
                "country": "IN",
                "jurisdiction_pack": pack,
            },
        )
        self.stdout.write(f"  Organisation: {tenant.name}")
        return tenant

    def _practice(self, pack: JurisdictionPack) -> Tenant:
        tenant, _ = Tenant.objects.get_or_create(
            slug="sharma-associates",
            defaults={
                "type": Tenant.Type.PRACTICE,
                "name": "Sharma & Associates, Chartered Accountants",
                "status": Tenant.Status.ACTIVE,
                "country": "IN",
                "jurisdiction_pack": pack,
            },
        )
        self.stdout.write(f"  Practice: {tenant.name}")
        return tenant

    def _users(self, org: Tenant, practice: Tenant) -> None:
        people = [
            (org, "org-owner", "priya@vaibhav-textiles.example", "Priya Vaibhav", "+919812340001"),
            (
                org,
                "org-compliance-manager",
                "ramesh@vaibhav-textiles.example",
                "Ramesh Patel",
                "+919812340002",
            ),
            (
                org,
                "org-department-user",
                "hitesh@vaibhav-textiles.example",
                "Hitesh Shah",
                "+919812340003",
            ),
            (
                practice,
                "practice-partner",
                "anand@sharma-associates.example",
                "Anand Sharma",
                "+919812340011",
            ),
            (
                practice,
                "practice-staff",
                "nikhil@sharma-associates.example",
                "Nikhil Rao",
                "+919812340012",
            ),
        ]

        for tenant, role_code, email, name, phone in people:
            role = Role.objects.filter(
                tenant__isnull=True, code=role_code, tenant_type=tenant.type
            ).first()
            if role is None:
                self.stderr.write(
                    self.style.ERROR(
                        f"  Missing system role {role_code!r}. Run `manage.py sync_system_roles` first."
                    )
                )
                raise SystemExit(1)

            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "full_name": name,
                    "phone_e164": phone,
                    # Pre-verified so the demo does not require reading codes out
                    # of the console before anything can be looked at.
                    "email_verified": True,
                    "phone_verified": True,
                },
            )
            if created:
                user.set_password(PASSWORD)
                user.save()

            Membership.objects.get_or_create(
                tenant=tenant,
                user=user,
                defaults={"role": role, "status": Membership.Status.ACTIVE},
            )
            self.stdout.write(f"    {email:<36} {role.name}")

    def _entities(self, org: Tenant) -> dict[str, Entity]:
        with tenant_context(tenant_ids=org.id, reason="seed_dev:entities"):
            textile, _ = Entity.objects.get_or_create(
                tenant=org,
                short_code="VTPL",
                defaults={
                    "name": "Vaibhav Textiles Pvt Ltd",
                    "legal_name": "Vaibhav Textiles Private Limited",
                    "entity_type": "PVT_LTD",
                    "country": "IN",
                    "incorporation_date": date(2011, 6, 14),
                    "registered_office_state": "IN-GJ",
                    "registered_office_address": "Ring Road, Surat, Gujarat 395002",
                },
            )
            software, _ = Entity.objects.get_or_create(
                tenant=org,
                short_code="VSOFT",
                defaults={
                    "name": "Vaibhav Software LLP",
                    "legal_name": "Vaibhav Software LLP",
                    "entity_type": "LLP",
                    "country": "IN",
                    "incorporation_date": date(2019, 2, 4),
                    "registered_office_state": "IN-KA",
                    "registered_office_address": "Indiranagar, Bengaluru, Karnataka 560038",
                },
            )

            EntityProfile.objects.get_or_create(
                entity=textile,
                defaults={
                    "tenant": org,
                    "aggregate_turnover": Decimal("805000000"),  # ₹80.5 Cr
                    "employee_count": 212,
                    "contractor_count": 64,
                    "paid_up_capital": Decimal("25000000"),
                    "nic_code": "13921",
                    "sector": "Manufacturing",
                    "sub_sector": "Textiles",
                    "states_of_operation": ["IN-GJ", "IN-MH"],
                    "facts": {
                        "gst_scheme": "REGULAR",
                        "qrmp_opted": False,
                        "is_listed": False,
                        "has_boiler": True,
                        "has_canteen": True,
                        "has_csr_obligation": True,
                        "has_msme_vendors": True,
                        "has_export_import": True,
                        "deals_in_hazardous_material": True,
                        "women_employees_count": 47,
                    },
                },
            )
            EntityProfile.objects.get_or_create(
                entity=software,
                defaults={
                    "tenant": org,
                    "aggregate_turnover": Decimal("47000000"),  # ₹4.7 Cr
                    "employee_count": 28,
                    "nic_code": "62011",
                    "sector": "Services",
                    "sub_sector": "Software development",
                    "states_of_operation": ["IN-KA"],
                    "facts": {
                        "gst_scheme": "REGULAR",
                        "qrmp_opted": True,
                        "has_export_import": True,
                        "is_startup_dpiit": True,
                        "has_esop": True,
                    },
                },
            )

            # Two GST registrations on one entity. This is the case that breaks
            # naive designs: it means two GSTR-3Bs every month, not one.
            for reg_type, value, jurisdiction in [
                ("PAN", "AAACV1234K", ""),
                ("TAN", "SRTV12345E", ""),
                ("GST", "24AAACV1234K1Z8", "IN-GJ"),
                ("GST", "27AAACV1234K1ZB", "IN-MH"),
                ("CIN", "U17110GJ2011PTC065432", ""),
            ]:
                EntityRegistration.objects.get_or_create(
                    entity=textile,
                    type=reg_type,
                    jurisdiction=jurisdiction,
                    defaults={"tenant": org, "value": value, "valid_from": date(2017, 7, 1)},
                )

            for reg_type, value, jurisdiction in [
                ("PAN", "AAEFV5678M", ""),
                ("GST", "29AAEFV5678M1Z4", "IN-KA"),
                ("LLPIN", "AAB-1234", ""),
            ]:
                EntityRegistration.objects.get_or_create(
                    entity=software,
                    type=reg_type,
                    jurisdiction=jurisdiction,
                    defaults={"tenant": org, "value": value, "valid_from": date(2019, 2, 4)},
                )

            # Two factories. Fire NOCs, pollution consents and boiler
            # inspections are per site, not per company.
            for name, premises_type, jurisdiction, facts in [
                (
                    "Surat Weaving Unit",
                    "FACTORY",
                    "IN-GJ",
                    {"has_boiler": True, "worker_count": 148},
                ),
                (
                    "Bhiwandi Processing Unit",
                    "FACTORY",
                    "IN-MH",
                    {"has_boiler": False, "worker_count": 64},
                ),
                ("Surat Head Office", "REGISTERED_OFFICE", "IN-GJ", {}),
            ]:
                EntityPremises.objects.get_or_create(
                    entity=textile,
                    name=name,
                    defaults={
                        "tenant": org,
                        "type": premises_type,
                        "jurisdiction": jurisdiction,
                        "facts": facts,
                    },
                )

            self.stdout.write(
                "  Entities: Vaibhav Textiles Pvt Ltd (2 GSTINs, 3 premises), Vaibhav Software LLP"
            )
            return {"textile": textile, "software": software}

    def _engagement(self, practice: Tenant, entity: Entity) -> None:
        """Engage the firm on ONE entity, limited to tax categories.

        Deliberately partial. If the demo engaged the practice on everything,
        scope narrowing would look like it works when in fact nothing had been
        narrowed — and the isolation tests would be the only thing exercising it.
        """
        with tenant_context(tenant_ids=entity.tenant_id, reason="seed_dev:engagement"):
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
                        "tenancy.profile.edit",
                        "tenancy.registration.view",
                        "core.search",
                    ],
                    "starts_on": date(2024, 4, 1),
                    "engagement_letter_ref": "SA/2024/VTPL/01",
                },
            )
        self.stdout.write(
            f"  Engagement: {practice.name} on {entity.name} "
            f"(tax and secretarial only — Vaibhav Software LLP is NOT visible to them)"
        )
