"""
Demo *work* for development: the state the modules are actually about.

`seed_dev` builds the world — tenants, entities, registrations, premises, an
engagement. This builds what has happened in it: a materialised compliance
calendar, a work board, an overdue invoice and a full notification inbox.

It lives here rather than in `seed_dev` because it imports several modules, and a
command in `tenancy` reaching into `billing` and `practice` inverts the
dependency the app list is careful about. `seed_dev` calls it at the end, so
``tasks.ps1 seed`` still does everything in one step.

**Everything is deliberately messy.** A demo where every obligation is on track
and every invoice paid demonstrates nothing: the screens that matter are the
ones showing an overdue filing and a client who has gone quiet, and those states
have to exist to be looked at. Anyone reviewing this product is going to open
the dashboard first, and a tidy dashboard is an empty one.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.scope import platform_scope, tenant_context
from stacos.tenancy.models import Entity, Tenant

TEXTILE_SLUG = "vaibhav-textiles"
PRACTICE_SLUG = "sharma-associates"


class Command(BaseCommand):
    help = "Populate the modules with realistic in-flight work for development."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--as-of",
            default="",
            help="ISO date the demo world is 'today'. Defaults to the real today.",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        as_of = date.fromisoformat(options["as_of"]) if options["as_of"] else timezone.localdate()

        with platform_scope(reason="seed_work"):
            org = Tenant.objects.filter(slug=TEXTILE_SLUG).first()
            practice = Tenant.objects.filter(slug=PRACTICE_SLUG).first()
            if org is None or practice is None:
                self.stderr.write(
                    self.style.ERROR("Run `manage.py seed_dev` first — no demo tenants found.")
                )
                return

            entities = {e.short_code or e.name: e for e in Entity.objects.filter(tenant=org)}
            textile = next(iter(entities.values()))
            people = {u.email: u for u in User.objects.filter(email__endswith=".example")}

        self._calendar(org, textile, as_of)
        self._practice(practice, org, people, as_of)
        self._billing(org, as_of)
        self._notifications(org, textile, people, as_of)

        self.stdout.write(self.style.SUCCESS("\nDemo work ready."))

    # -- Compliance calendar -------------------------------------------------

    def _calendar(self, org: Tenant, entity: Entity, as_of: date) -> None:
        """Materialise the register, then move some of it on.

        Through the real planner rather than by writing rows: a seeded calendar
        that did not come out of the engine would hide exactly the bugs a demo is
        supposed to surface, and would drift from the catalog the first time a
        rule changed.
        """
        from stacos.engine.lifecycle import State
        from stacos.obligations.models import ObligationInstance
        from stacos.obligations.services import StaleCatalogError, materialise

        with tenant_context(tenant_ids=org.pk, reason="seed_work:calendar"):
            try:
                run = materialise(entity, as_of=as_of, trigger="MANUAL")
            except StaleCatalogError:
                self.stdout.write(
                    self.style.WARNING(
                        "  Calendar: catalog not loaded — run `manage.py loadcatalog` first."
                    )
                )
                return

            rows = list(
                ObligationInstance.objects.filter(entity=entity, state=State.NOT_STARTED).order_by(
                    "due_date"
                )
            )

            # A spread of states, so every colour on the dashboard has something
            # behind it and the empty states are not the only thing on show.
            moved = 0
            for index, obligation in enumerate(rows):
                if obligation.due_date is None:
                    continue
                if obligation.due_date < as_of - timedelta(days=20):
                    obligation.state = State.FILED
                    obligation.filed_on = obligation.due_date
                    obligation.filing_reference = f"AA24{index:08d}"
                elif obligation.due_date < as_of:
                    # Left open past its date on purpose. The overdue tile is the
                    # first thing anyone looks at.
                    continue
                elif index % 4 == 0:
                    obligation.state = State.IN_PREPARATION
                elif index % 7 == 0:
                    obligation.state = State.PENDING_REVIEW
                else:
                    continue
                obligation.save()
                moved += 1

            overdue = ObligationInstance.objects.filter(
                entity=entity, state__in=[State.NOT_STARTED, State.IN_PREPARATION]
            ).filter(due_date__lt=as_of)

            self.stdout.write(
                f"  Calendar: {run.summary()}; {moved} moved on, {overdue.count()} left overdue"
            )

    # -- Practice ------------------------------------------------------------

    def _practice(
        self, practice: Tenant, client: Tenant, people: dict[str, User], as_of: date
    ) -> None:
        """A board with something in every column, and time logged against it."""
        from stacos.practice.models import RateCard, TimeEntry, WorkItem, WorkItemState

        partner = people.get("anand@sharma-associates.example")
        staff = people.get("nikhil@sharma-associates.example")

        with tenant_context(tenant_ids=practice.pk, reason="seed_work:practice"):
            # A rate per person, not one house rate: a partner's hour and a
            # junior's hour are not worth the same, and the WIP figure is only
            # meaningful if that is true in the data.
            for person, rate in ((partner, Decimal("4500.00")), (staff, Decimal("1800.00"))):
                if person is None:
                    continue
                RateCard.objects.get_or_create(
                    tenant=practice,
                    user=person,
                    valid_from=date(as_of.year, 4, 1),
                    defaults={"rate": rate, "note": "Standard rate card 2026-27"},
                )

            plan = [
                ("GSTR-3B August — Vaibhav Textiles", WorkItemState.IN_PROGRESS, staff, 3),
                ("Reply to 143(2) scrutiny notice", WorkItemState.IN_REVIEW, partner, 11),
                ("TDS return Q2 preparation", WorkItemState.ASSIGNED, staff, 20),
                ("Annual ROC filings — AOC-4", WorkItemState.BACKLOG, None, 60),
                ("Chase August closing documents", WorkItemState.BLOCKED, staff, -2),
            ]

            created = 0
            for title, state, assignee, due_in in plan:
                item, was_created = WorkItem.objects.get_or_create(
                    tenant=practice,
                    title=title,
                    defaults={
                        "client_tenant": client,
                        "state": state,
                        "assigned_to": assignee,
                        "due_on": as_of + timedelta(days=due_in),
                        "blocked_reason": (
                            "Waiting on the client for the purchase register."
                            if state == WorkItemState.BLOCKED
                            else ""
                        ),
                    },
                )
                created += int(was_created)
                if was_created and assignee is not None:
                    TimeEntry.objects.create(
                        tenant=practice,
                        work_item=item,
                        client_tenant=client,
                        user=assignee,
                        worked_on=as_of - timedelta(days=1),
                        hours=Decimal("2.5"),
                        rate=Decimal("4500.00") if assignee is partner else Decimal("1800.00"),
                        narrative="Preparation and review.",
                        is_billable=True,
                    )

            self.stdout.write(f"  Practice: {created} work items across five columns, time logged")

    # -- Billing -------------------------------------------------------------

    def _billing(self, org: Tenant, as_of: date) -> None:
        """A live subscription, one paid invoice and one overdue.

        The overdue one exists so the dunning state is visible. A billing screen
        where everything is paid tells you nothing about what the module does.
        """
        from stacos.billing.models import Invoice, Payment, Plan, Subscription, to_minor
        from stacos.billing.services import issue_invoice, record_payment

        with platform_scope(reason="seed_work:plan"):
            plan, _ = Plan.objects.get_or_create(
                code="growth",
                defaults={
                    "name": "Growth",
                    "tenant_type": Tenant.Type.ORGANISATION,
                    "amount_minor": to_minor(Decimal("7500")),
                    "interval": Plan.Interval.MONTHLY,
                    "included_entities": 10,
                    "included_users": 25,
                    "features": [
                        "Compliance calendar for up to 10 entities",
                        "WhatsApp and email reminders",
                    ],
                    "is_active": True,
                    "is_public": True,
                },
            )

        with tenant_context(tenant_ids=org.pk, reason="seed_work:billing"):
            subscription, created = Subscription.objects.get_or_create(
                tenant=org,
                defaults={
                    "plan": plan,
                    "status": Subscription.Status.ACTIVE,
                    "started_on": date(as_of.year, 4, 1),
                    "current_period_start": as_of.replace(day=1),
                    "current_period_end": as_of.replace(day=1) + timedelta(days=30),
                },
            )
            if not created:
                self.stdout.write("  Billing: already present")
                return

            # Last month's, still unpaid — the state the dunning screen is for.
            overdue = issue_invoice(subscription, issued_on=as_of - timedelta(days=39))
            Invoice.objects.filter(pk=overdue.pk).update(
                due_on=as_of - timedelta(days=9), status=Invoice.Status.OVERDUE
            )

            # This month's, settled by transfer — still the majority of Indian
            # B2B collection, and the path that needs step-up to record.
            paid = issue_invoice(subscription, issued_on=as_of - timedelta(days=9))
            record_payment(
                paid,
                amount_minor=paid.total_minor,
                method=Payment.Method.OFFLINE,
                external_reference="NEFT/2026/08/8841",
            )

            self.stdout.write("  Billing: Growth plan, 1 invoice paid, 1 nine days overdue")

    # -- Notifications -------------------------------------------------------

    def _notifications(
        self, org: Tenant, entity: Entity, people: dict[str, User], as_of: date
    ) -> None:
        """Run the real sweeps rather than writing rows.

        Same reasoning as the calendar: notifications written by hand would look
        right and prove nothing. Running the sweeps means the demo inbox is
        exactly what the product would have produced, ladder and all.
        """
        from stacos.notifications.models import Notification
        from stacos.notifications.tasks import sweep_billing, sweep_obligation_reminders

        for sweep in (sweep_obligation_reminders, sweep_billing):
            sweep(tenant_id=str(org.pk), as_of=as_of.isoformat())

        with tenant_context(tenant_ids=org.pk, reason="seed_work:notifications"):
            total = Notification.objects.count()
            unread = Notification.objects.filter(read_at__isnull=True).count()

        self.stdout.write(f"  Notifications: {total} raised by the sweeps, {unread} unread")
