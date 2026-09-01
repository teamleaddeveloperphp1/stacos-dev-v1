"""
Demo *work* for development: the state the modules are actually about.

`seed_dev` builds the world — tenants, entities, registrations, premises, an
engagement. This builds what has happened in it: a materialised compliance
calendar, documents in the vault, a half-answered information request, an open
notice with a fortnight to run, a return waiting for a second pair of eyes, a
work board, an overdue invoice and a full notification inbox.

It lives here rather than in `seed_dev` because it imports nine modules, and a
command in `tenancy` reaching into `billing` and `practice` inverts the
dependency the app list is careful about. `seed_dev` calls it at the end, so
``tasks.ps1 seed`` still does everything in one step.

**Everything is deliberately messy.** A demo where every obligation is on track,
every request answered and every invoice paid demonstrates nothing: the screens
that matter are the ones showing an overdue filing, a client who has gone quiet
and a document that failed its scan, and those states have to exist to be looked
at. Anyone reviewing this product is going to open the dashboard first, and a
tidy dashboard is an empty one.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
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
        self._vault(org, textile, people)
        self._requests(org, textile, people, as_of)
        self._notices(org, textile, people, as_of)
        self._returns(org, textile, people, as_of)
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

    # -- Vault ---------------------------------------------------------------

    def _vault(self, org: Tenant, entity: Entity, people: dict[str, User]) -> None:
        """Three documents, one of which failed its scan.

        The quarantined one is the point. Every demo vault is full of clean
        files, and then nobody ever sees what the product does when a client
        uploads something infected — which is the one behaviour worth showing.

        That last row is written as a *verdict* rather than produced by scanning
        real EICAR bytes, and the reason is practical: EICAR is designed to be
        detected, so writing it to a developer's disk means Windows Defender (or
        any desktop antivirus) removes the file from under Django and the seed
        dies on a file handle that was valid a millisecond earlier. The test
        suite does use real EICAR — through in-memory storage, where nothing
        touches the filesystem. Here the screen state is what matters.
        """
        from stacos.vault.models import Document, DocumentKind, ScanState
        from stacos.vault.services import store
        from stacos.vault.tasks import scan_document

        actor = next(iter(people.values()), None)

        with tenant_context(tenant_ids=org.pk, reason="seed_work:vault"):
            specs = [
                ("GSTR-3B July 2026 acknowledgement.pdf", DocumentKind.RETURN, b"%PDF-1.4 ack"),
                ("TDS challan Q1.pdf", DocumentKind.CHALLAN, b"%PDF-1.4 challan"),
                ("Bank statement July.pdf", DocumentKind.FINANCIAL, b"%PDF-1.4 statement"),
                ("Unknown attachment.pdf", DocumentKind.CORRESPONDENCE, b"%PDF-1.4 unknown"),
            ]

            for name, kind, content in specs:
                document, created = store(
                    tenant=org,
                    entity=entity,
                    upload=SimpleUploadedFile(name, content, content_type="application/pdf"),
                    kind=kind,
                    actor=actor,
                )
                if not created:
                    continue

                if name.startswith("Unknown"):
                    Document.objects.filter(pk=document.pk).update(
                        scan_state=ScanState.INFECTED,
                        scan_result="clamav:Pdf.Exploit.CVE_2023_26369",
                        scanned_at=timezone.now(),
                    )
                    continue

                # The scan normally runs on a worker. Running it inline keeps the
                # seed self-contained — with no broker, the queued message would
                # never be picked up and every file would sit at PENDING.
                scan_document(tenant_id=str(org.pk), document_id=str(document.pk))

            clean = Document.objects.filter(scan_state=ScanState.CLEAN).count()
            held = Document.objects.filter(scan_state=ScanState.INFECTED).count()
            self.stdout.write(f"  Vault: {clean} scanned clean, {held} quarantined")

    # -- Information requests ------------------------------------------------

    def _requests(self, org: Tenant, entity: Entity, people: dict[str, User], as_of: date) -> None:
        """One request, half answered, already overdue.

        Half-answered because "six documents asked for, five returned" is the
        state the whole module exists to represent, and a request that is either
        empty or complete never exercises it.
        """
        from stacos.requests.models import InformationRequest, RequestItem, RequestState
        from stacos.requests.services import record_response, send_request

        owner = people.get("priya@vaibhav-textiles.example")
        manager = people.get("ramesh@vaibhav-textiles.example")

        with tenant_context(tenant_ids=org.pk, reason="seed_work:requests"):
            request, created = InformationRequest.objects.get_or_create(
                entity=entity,
                title="August closing — supporting documents",
                defaults={
                    "tenant": org,
                    "message": (
                        "Please send these before the 15th so we can file GSTR-3B on time."
                    ),
                    "due_on": as_of - timedelta(days=2),
                    "requested_by": manager,
                    "assigned_to": owner,
                    "state": RequestState.DRAFT,
                },
            )
            if not created:
                self.stdout.write("  Requests: already present")
                return

            items = [
                ("Bank statement — August", RequestItem.Kind.DOCUMENT),
                ("Purchase register", RequestItem.Kind.DOCUMENT),
                ("Closing stock value", RequestItem.Kind.DATA),
                ("Confirm no exports this month", RequestItem.Kind.CONFIRMATION),
            ]
            created_items = [
                RequestItem.objects.create(
                    tenant=org,
                    entity=entity,
                    request=request,
                    label=label,
                    kind=kind,
                    ordinal=index,
                )
                for index, (label, kind) in enumerate(items, start=1)
            ]

            send_request(request, actor=manager)

            record_response(created_items[2], value="₹41,20,000", actor=owner)
            record_response(created_items[3], value="Confirmed — no exports", actor=owner)

            request.refresh_from_db()
            outstanding = sum(1 for item in request.items.all() if not item.is_answered)
            self.stdout.write(
                f"  Requests: 1 sent, {outstanding} of {len(items)} still outstanding, "
                f"2 days overdue"
            )

    # -- Notices -------------------------------------------------------------

    def _notices(self, org: Tenant, entity: Entity, people: dict[str, User], as_of: date) -> None:
        """A live scrutiny notice with a fortnight left, and a closed one behind it."""
        from stacos.jurisdictions.models import Authority
        from stacos.notices.models import Notice, NoticeState, NoticeType

        manager = people.get("ramesh@vaibhav-textiles.example")

        with platform_scope(reason="seed_work:authority"):
            authority = Authority.objects.filter(code__icontains="IT").first()
            if authority is None:
                authority = Authority.objects.first()
        if authority is None:
            self.stdout.write(self.style.WARNING("  Notices: no authorities loaded — skipped"))
            return

        with tenant_context(tenant_ids=org.pk, reason="seed_work:notices"):
            Notice.objects.get_or_create(
                entity=entity,
                authority=authority,
                reference_number="ITBA/AST/S/143(2)/2026-27/1052841",
                defaults={
                    "tenant": org,
                    "notice_type": NoticeType.SCRUTINY,
                    "statutory_reference": "Section 143(2), Income-tax Act 1961",
                    "subject": "Limited scrutiny — mismatch in turnover reported in GSTR-9 and ITR",
                    "summary": (
                        "Explain the difference between turnover per GSTR-9 (₹18.4 crore) "
                        "and per the return of income (₹17.9 crore) for AY 2025-26."
                    ),
                    "financial_years": "2024-25",
                    "issued_on": as_of - timedelta(days=6),
                    "received_on": as_of - timedelta(days=4),
                    # Read off the notice. Fifteen days, not the thirty a rule
                    # would have assumed — which is why this field is not computed.
                    "respond_by": as_of + timedelta(days=11),
                    "demand_amount": None,
                    "state": NoticeState.UNDER_REVIEW,
                    "risk": Notice.Risk.HIGH,
                    "assigned_to": manager,
                },
            )

            Notice.objects.get_or_create(
                entity=entity,
                authority=authority,
                reference_number="GST/ASMT-10/2025-26/00417",
                defaults={
                    "tenant": org,
                    "notice_type": NoticeType.MISMATCH,
                    "statutory_reference": "Rule 99, CGST Rules 2017",
                    "subject": "Scrutiny of returns — ITC mismatch with GSTR-2B",
                    "issued_on": as_of - timedelta(days=95),
                    "received_on": as_of - timedelta(days=92),
                    "respond_by": as_of - timedelta(days=62),
                    "demand_amount": Decimal("284500.00"),
                    "accepted_amount": Decimal("41200.00"),
                    "state": NoticeState.CLOSED,
                    "risk": Notice.Risk.MEDIUM,
                    "assigned_to": manager,
                },
            )
            self.stdout.write("  Notices: 1 open (11 days to respond), 1 closed")

    # -- Returns -------------------------------------------------------------

    def _returns(self, org: Tenant, entity: Entity, people: dict[str, User], as_of: date) -> None:
        """A return prepared by one person and waiting for another.

        Maker-checker is enforced by a database constraint, so the demo has to
        use two different people — which is also the only way to see the review
        queue with anything in it.
        """
        from stacos.engine.lifecycle import State
        from stacos.obligations.models import ObligationInstance
        from stacos.returns.models import PreparationState, ReturnPreparation

        preparer = people.get("ramesh@vaibhav-textiles.example")
        with tenant_context(tenant_ids=org.pk, reason="seed_work:returns"):
            # A preparation is the working papers *for an obligation* — the
            # one-to-one is what stops two sets of figures existing for the same
            # filing. So the demo attaches to a real calendar entry rather than
            # inventing a free-floating return.
            obligation = (
                ObligationInstance.objects.filter(
                    entity=entity,
                    state=State.PENDING_REVIEW,
                    preparation__isnull=True,
                )
                .order_by("due_date")
                .first()
            )
            if obligation is None:
                self.stdout.write("  Returns: no obligation awaiting review — skipped")
                return

            _, created = ReturnPreparation.objects.get_or_create(
                obligation=obligation,
                defaults={
                    "entity": entity,
                    "form_type": obligation.title[:40],
                    "period_key": obligation.period_key,
                    "tenant": org,
                    # PREPARED, not REVIEWED: the maker has finished and the
                    # checker has not looked. That is the state the review queue
                    # is built to show, and a database constraint stops the same
                    # person occupying both roles.
                    "state": PreparationState.PREPARED,
                    "prepared_by": preparer,
                    "prepared_at": timezone.now() - timedelta(days=1),
                },
            )
            self.stdout.write(
                f"  Returns: {'1 waiting for review' if created else 'already present'}"
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
                        "Document vault with virus scanning",
                        "Notices and information requests",
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
        from stacos.notifications.tasks import (
            sweep_billing,
            sweep_notice_deadlines,
            sweep_obligation_reminders,
            sweep_request_reminders,
        )

        for sweep in (
            sweep_obligation_reminders,
            sweep_request_reminders,
            sweep_notice_deadlines,
            sweep_billing,
        ):
            sweep(tenant_id=str(org.pk), as_of=as_of.isoformat())

        with tenant_context(tenant_ids=org.pk, reason="seed_work:notifications"):
            total = Notification.objects.count()
            unread = Notification.objects.filter(read_at__isnull=True).count()

        self.stdout.write(f"  Notifications: {total} raised by the sweeps, {unread} unread")
