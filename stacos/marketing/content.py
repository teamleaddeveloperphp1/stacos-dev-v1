"""
The public surface, as data.

Every marketing page is a rendering of a value in this module. That is not
decoration: the navigation, the footer, the sitemap and the page bodies all have
to agree about what pages exist, and the only way they stay in agreement as the
site grows is for one of them to be the source and the rest to be derived.

Three consequences worth knowing before editing:

* **A page exists once you add it here and route it.** ``NAV`` and ``FOOTER``
  are built from these tuples, so a new module or guide appears in the header,
  the footer and ``sitemap.xml`` without touching a template.
* **Copy lives here, not in the templates.** Templates loop; they do not narrate.
  A marketing edit is a diff a non-templating reader can follow.
* **Nothing here is a compliance rule.** Jurisdiction facts, applicability and
  due dates come from a ``JurisdictionPack`` evaluated by ``stacos.engine``
  (CLAUDE.md rule 4). This module holds prose *about* the product; it never
  decides anything. Statute names appear only as examples in sentences.

Claims discipline: this page set is handed to procurement teams. Nothing here
asserts a certification we do not hold, a customer we do not have, or a number we
cannot evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

__all__ = [
    "ADD_ONS",
    "AUDIENCES",
    "CHANGELOG",
    "COMPANY",
    "COMPARISON_ROWS",
    "FAQ_SECTIONS",
    "FOOTER",
    "GUIDES",
    "LEGAL_DOCUMENTS",
    "MODULES",
    "NAV",
    "OPEN_ROLES",
    "PARTNER_TRACKS",
    "PLANS",
    "PRICING_FAQS",
    "SUBPROCESSORS",
    "Audience",
    "Faq",
    "Guide",
    "LegalDocument",
    "Module",
    "NavItem",
    "NavSection",
    "Plan",
    "audience_by_slug",
    "guide_by_slug",
    "legal_by_slug",
    "module_by_slug",
]


# ===========================================================================
# Types
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Section:
    """A heading plus its paragraphs. The unit every long page is built from."""

    heading: str
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Faq:
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class FaqSection:
    title: str
    faqs: tuple[Faq, ...]


@dataclass(frozen=True, slots=True)
class Module:
    """One product area, with its own page under ``/product/``."""

    slug: str
    name: str
    nav_summary: str
    icon: str
    headline: str
    lede: str
    audience: str
    highlights: tuple[tuple[str, str], ...] = ()
    sections: tuple[Section, ...] = ()
    faqs: tuple[Faq, ...] = ()

    @property
    def url_name(self) -> str:
        return "marketing:module"


@dataclass(frozen=True, slots=True)
class Audience:
    """A "who is this for" page: business, practice, enterprise."""

    slug: str
    name: str
    nav_summary: str
    headline: str
    lede: str
    pains: tuple[tuple[str, str], ...] = ()
    outcomes: tuple[tuple[str, str], ...] = ()
    module_slugs: tuple[str, ...] = ()
    plan_code: str = "business"
    sections: tuple[Section, ...] = ()
    faqs: tuple[Faq, ...] = ()


@dataclass(frozen=True, slots=True)
class Guide:
    """Long-form explanatory content. The reason anyone arrives from a search."""

    slug: str
    title: str
    summary: str
    published: date
    updated: date
    minutes: int
    topic: str
    sections: tuple[Section, ...] = ()
    faqs: tuple[Faq, ...] = ()
    related_slugs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LegalDocument:
    """Metadata for a legal page. The prose itself is a template."""

    slug: str
    title: str
    summary: str
    effective: date
    template: str
    footer: bool = True


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    name: str
    tagline: str
    monthly_inr: Decimal
    annual_inr: Decimal
    included_entities: int
    included_users: int
    features: tuple[str, ...] = field(default_factory=tuple)
    highlight: bool = False
    cta: str = "Start free trial"

    @property
    def annual_saving_pct(self) -> int:
        """How much the annual commitment saves against paying monthly."""
        if not self.monthly_inr:
            return 0
        full = self.monthly_inr * 12
        return round((full - self.annual_inr) / full * 100)


@dataclass(frozen=True, slots=True)
class NavItem:
    label: str
    url_name: str
    args: tuple[str, ...] = ()
    summary: str = ""
    icon: str = ""


@dataclass(frozen=True, slots=True)
class NavSection:
    label: str
    items: tuple[NavItem, ...]
    footer_note: str = ""


# ===========================================================================
# The company
#
# One address, one set of contact routes, one grievance officer. Repeating any
# of these in a template is how a site ends up with two support addresses, one
# of which nobody reads.
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Company:
    legal_name: str = "Skorydov Systems Private Limited"
    product_name: str = "STACOS"
    product_expansion: str = "Skorydov Tax and Compliance Software"
    tagline: str = "Every compliance obligation you have, worked out for you."
    founded: int = 2024
    address_lines: tuple[str, ...] = (
        "Skorydov Systems Private Limited",
        "Andheri East",
        "Mumbai 400069",
        "Maharashtra, India",
    )
    support_email: str = "support@stacos.in"
    sales_email: str = "sales@stacos.in"
    security_email: str = "security@stacos.in"
    privacy_email: str = "privacy@stacos.in"
    careers_email: str = "careers@stacos.in"
    partners_email: str = "partners@stacos.in"
    grievance_officer: str = "Grievance Officer, Skorydov Systems Private Limited"
    grievance_email: str = "grievance@stacos.in"
    support_hours: str = "Monday to Friday, 10:00–19:00 IST, excluding public holidays"
    linkedin_url: str = "https://www.linkedin.com/company/skorydov"
    youtube_url: str = "https://www.youtube.com/@skorydov"
    x_url: str = "https://x.com/skorydov"


COMPANY = Company()


# ===========================================================================
# Product modules
# ===========================================================================

MODULES: tuple[Module, ...] = (
    Module(
        slug="compliance-calendar",
        name="Compliance calendar",
        nav_summary="Your statutory calendar, generated from your profile.",
        icon="calendar",
        headline="The calendar builds itself from what your business is.",
        lede=(
            "You answer a short profile — entity type, the states you operate in, "
            "industry, turnover band, headcount, a handful of flags. Every obligation "
            "that applies to you is derived from that, with its own due date, owner "
            "and evidence trail. You start from a full calendar and edit exceptions, "
            "never from an empty page."
        ),
        audience="Businesses and professional firms",
        highlights=(
            (
                "Applicability, not a template",
                "Two manufacturers in the same state with different headcounts owe "
                "different things. Applicability is evaluated per entity against your "
                "profile rather than handed to you as one generic checklist.",
            ),
            (
                "Per-registration fan-out",
                "A company with registrations in four states owes the same monthly "
                "return four times, on four separate rows, with four separate owners. "
                "One row for “the GST return” is how a state gets missed.",
            ),
            (
                "Extensions without amnesia",
                "When an authority extends a date, the row shows the extended date "
                "and keeps the original visible beside it. What the statute said "
                "still matters after the extension lapses.",
            ),
            (
                "Owners, not a shared inbox",
                "Every obligation has a named internal owner and, where an engagement "
                "covers it, a named preparer at your firm. Unassigned work is visible "
                "as unassigned rather than quietly assumed.",
            ),
        ),
        sections=(
            Section(
                heading="How a calendar gets generated",
                paragraphs=(
                    "Your entity profile is a set of facts: constitution, registered "
                    "and operating states, industry, turnover band, employee count, "
                    "whether you import or export, whether you run a factory, whether "
                    "you are listed. Facts, not opinions.",
                    "Those facts are evaluated against a versioned rule set that says "
                    "which obligations apply and how each due date is computed. When "
                    "a rule changes, the new version applies from its own effective "
                    "date; periods you have already closed keep the rule that was in "
                    "force when they were closed.",
                ),
                bullets=(
                    "Change a fact — a new state, a new factory licence — and the "
                    "calendar re-derives, showing you exactly what was added or removed.",
                    "Every generated row records which rule version produced it.",
                    "Manual additions are allowed and marked as manual, so an auditor "
                    "can tell derived work from ad-hoc work.",
                ),
            ),
            Section(
                heading="What a row carries",
                paragraphs=(
                    "An obligation is not a to-do. Each row holds the period it "
                    "covers, the statutory date, any extended date, the registration "
                    "or premises it belongs to, the owner, the preparer, the current "
                    "status in the shared vocabulary, the documents filed against it, "
                    "and the full history of who changed what.",
                ),
            ),
            Section(
                heading="Reminders that escalate",
                paragraphs=(
                    "Reminders run on a schedule you set per obligation class, not one "
                    "global setting: a monthly return warns at a different distance "
                    "than an annual filing. They escalate to the owner’s manager when "
                    "a date passes, and they stop the moment the row is closed.",
                    "Channels are email, in-app, mobile push, and — where you have "
                    "registered templates — WhatsApp and SMS.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="What if the generated calendar is wrong for my business?",
                answer=(
                    "Mark the obligation not applicable with a reason. The reason is "
                    "recorded, the row stays visible as a deliberate exclusion, and "
                    "it is not silently regenerated. If the underlying profile fact "
                    "was wrong, fix the fact and the calendar re-derives."
                ),
            ),
            Faq(
                question="Do you file returns on my behalf?",
                answer=(
                    "No. STACOS tracks, prepares and evidences. Filing happens on the "
                    "relevant portal by the person authorised to do it, and the "
                    "acknowledgement is attached to the obligation as proof."
                ),
            ),
        ),
    ),
    Module(
        slug="notices",
        name="Notices and litigation",
        nav_summary="Every notice, its deadline, and what you did about it.",
        icon="alert",
        headline="A notice with a deadline is the most expensive thing to lose.",
        lede=(
            "Notices arrive by post, by email, and on portals nobody checks daily. "
            "The notice tracker holds every one of them with the authority, the "
            "section, the period, the response deadline, the responsible person, and "
            "the complete response history — including what was attached and when."
        ),
        audience="Businesses and professional firms",
        highlights=(
            (
                "Deadline countdown",
                "Response windows are short and count in days. Every notice shows the "
                "days remaining in the same colour and word vocabulary the rest of the "
                "product uses for lateness.",
            ),
            (
                "Adjournments recorded",
                "An extension is a fact with a date and a document, not a change to "
                "the original deadline. Both are held.",
            ),
            (
                "Threaded responses",
                "Draft, review, approve, submit. Each step names a person and a time, "
                "and the submitted bundle is frozen so it can be reproduced later.",
            ),
            (
                "Escalation to litigation",
                "When a notice becomes an appeal, it keeps its history: the same "
                "record carries forward, with its own hearing dates and forum.",
            ),
        ),
        sections=(
            Section(
                heading="Nothing arrives in one place, so intake is deliberate",
                paragraphs=(
                    "You can create a notice from an upload, from an email forwarded "
                    "to your account’s intake address, or by hand. Whichever route it "
                    "takes, the record is the same, and the original document is kept "
                    "unaltered alongside anything typed from it.",
                ),
            ),
            Section(
                heading="Who is allowed to see it",
                paragraphs=(
                    "Notices are among the most sensitive records a business holds. "
                    "They are scoped to an entity and a compliance category like "
                    "everything else, so a firm engaged only on payroll matters does "
                    "not see a tax demand, and a department-scoped internal user does "
                    "not see another department’s.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="Can our counsel be given access to one matter only?",
                answer=(
                    "Yes. An engagement can be scoped to named entities and named "
                    "compliance categories, with a start and an end date. Ending it "
                    "removes access on the same request."
                ),
            ),
        ),
    ),
    Module(
        slug="documents",
        name="Document vault",
        nav_summary="Filings, acknowledgements and evidence, versioned.",
        icon="folder",
        headline="The filing is not done until the acknowledgement is filed too.",
        lede=(
            "Every document lives against the thing it belongs to — an obligation, a "
            "notice, an entity, a registration, a person — with versions, retention "
            "class and an access log. A shared drive can hold your documents. It "
            "cannot tell you which challan proves which period."
        ),
        audience="Businesses and professional firms",
        highlights=(
            (
                "Filed against a record, not a folder",
                "Attachments hang off the obligation or notice they evidence, so the "
                "proof and the claim never drift apart.",
            ),
            (
                "Versions kept",
                "Replacing a document supersedes it rather than deleting it. The "
                "earlier version stays reachable with its own upload record.",
            ),
            (
                "Retention classes",
                "Statutory registers, tax records and employee documents have "
                "different retention periods. Each class carries its own schedule.",
            ),
            (
                "Access is logged",
                "Who downloaded which document, when, from which address. Available as an export.",
            ),
        ),
        sections=(
            Section(
                heading="Requesting documents from other people",
                paragraphs=(
                    "Most missing documents are missing because someone was asked over "
                    "WhatsApp and forgot. An information request names the document, "
                    "the person, the due date and the obligation it unblocks; it "
                    "reminds on a schedule, escalates when ignored, and files the "
                    "response straight into the vault against the right record.",
                ),
            ),
        ),
    ),
    Module(
        slug="registers",
        name="Registers and secretarial",
        nav_summary="Statutory registers, meetings and minutes.",
        icon="building",
        headline="The registers a company is required to keep, kept properly.",
        lede=(
            "Members, directors, charges, related-party contracts. Board and general "
            "meetings with notice periods, quorum, attendance, resolutions and "
            "minutes. Maintained continuously rather than reconstructed the week "
            "before a due-diligence request."
        ),
        audience="Companies, LLPs and the firms that serve them",
        highlights=(
            (
                "Registers update from events",
                "An allotment, a transfer, a change of directorship writes to the "
                "register as a dated entry rather than by editing a spreadsheet cell.",
            ),
            (
                "Meetings with their paperwork",
                "Notice, agenda, attendance, resolutions and signed minutes held "
                "together, with the dates that make each of them valid.",
            ),
            (
                "Signatory and shareholding history",
                "Who could sign what, on any past date — the question every diligence "
                "exercise asks and few companies can answer quickly.",
            ),
            (
                "Export for diligence",
                "A dated, complete pack rather than a scramble through email.",
            ),
        ),
    ),
    Module(
        slug="collaboration",
        name="Working with your CA",
        nav_summary="Scoped, revocable access for your professional firm.",
        icon="shield",
        headline="Your CA works inside the system, not over WhatsApp.",
        lede=(
            "An engagement is a record your organisation approves: a named firm, "
            "named entities, named compliance categories, a start date and an end "
            "date. It is the only route by which one organisation sees another’s "
            "data, and it is revocable in one action."
        ),
        audience="Businesses and professional firms",
        highlights=(
            (
                "You approve, they prepare",
                "Work is prepared by the firm and approved by you. Nothing is filed "
                "unilaterally, and the approval is part of the audit trail.",
            ),
            (
                "Scoped to categories",
                "A payroll engagement does not see your tax notices. A tax engagement "
                "does not see board minutes.",
            ),
            (
                "Ends when it ends",
                "Access stops on the same request that ends the engagement — not on a "
                "nightly job — and you keep everything, including what the firm "
                "prepared.",
            ),
            (
                "Change firms without losing history",
                "The record belongs to the business. A new firm starts with the full "
                "history rather than an empty workspace and a folder of PDFs.",
            ),
        ),
        sections=(
            Section(
                heading="Why this is a record and not a permission checkbox",
                paragraphs=(
                    "Cross-organisation access is the single most dangerous thing this "
                    "product does. Making it a first-class, approved, time-boxed "
                    "record means every access has a reason, a scope and an end — and "
                    "means a client can answer “who at my CA firm can see my data, and "
                    "since when” without asking anyone.",
                ),
            ),
        ),
    ),
    Module(
        slug="practice",
        name="Practice management",
        nav_summary="Clients, work allocation, time, WIP and billing.",
        icon="briefcase",
        headline="Run the firm, not just the filings.",
        lede=(
            "Every client entity, every obligation across the whole book, on one "
            "board ranked by what is closest to breaching. Then the parts that decide "
            "whether the firm makes money: allocation, capacity, time, work in "
            "progress, and billing."
        ),
        audience="Chartered accountants, company secretaries, cost accountants, tax "
        "consultants and law firms",
        highlights=(
            (
                "One board, every client",
                "Sorted by risk rather than by client name, because the client you "
                "hear from least is usually the one about to breach.",
            ),
            (
                "Capacity you can see",
                "A heatmap of who is loaded and when, so allocation is a decision "
                "rather than a habit.",
            ),
            (
                "Maker–checker on preparation",
                "Preparation and review are separate roles with separate records. "
                "That is what makes a review defensible.",
            ),
            (
                "Realisation and recovery",
                "Time logged against a client and an obligation becomes work in "
                "progress, then an invoice, then a recovery percentage — the report "
                "that changes how a firm quotes next year.",
            ),
        ),
        sections=(
            Section(
                heading="Firm-wide templates",
                paragraphs=(
                    "A firm’s method is its asset. Checklists, working papers and "
                    "review steps are defined once at the firm and applied to every "
                    "engagement of that type, so quality does not depend on which "
                    "associate picked up the file.",
                ),
            ),
            Section(
                heading="Client onboarding without the first three weeks",
                paragraphs=(
                    "A new client’s calendar generates from their profile the same way "
                    "yours does. The first conversation is about exceptions, not about "
                    "building a list from scratch in a spreadsheet.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="Can a client see our internal time and margins?",
                answer=(
                    "No. Time, WIP, rates and profitability belong to the firm’s own "
                    "tenant. An engagement gives the firm scoped access to the "
                    "client’s records; it does not give the client access to the "
                    "firm’s."
                ),
            ),
        ),
    ),
    Module(
        slug="audit-trail",
        name="Audit trail",
        nav_summary="Append-only history of every state change.",
        icon="history",
        headline="The record you hand a regulator, or an acquirer.",
        lede=(
            "Every state change is recorded with the person, the time, the network "
            "address and what changed. The audit table is append-only: the database "
            "itself rejects updates and deletes, so the history cannot be quietly "
            "revised — including by us."
        ),
        audience="Everyone; especially finance leadership and auditors",
        highlights=(
            (
                "Append-only at the database",
                "A trigger denies UPDATE and DELETE on the audit table. This is a "
                "property of the schema, not a promise in a policy document.",
            ),
            (
                "Before and after",
                "Not just “changed” — the previous value and the new one.",
            ),
            (
                "Exportable",
                "Your full trail, on demand, in a format you can hand to someone else.",
            ),
            (
                "Support access is in it too",
                "Consented, time-boxed support sessions appear in your trail like any "
                "other actor. There is no silent impersonation.",
            ),
        ),
    ),
    Module(
        slug="mobile",
        name="Mobile app",
        nav_summary="Approvals and deadlines on Android and iOS.",
        icon="phone",
        headline="Approve from the airport. Do the work at a desk.",
        lede=(
            "The mobile app is deliberately narrow: what is due, what needs your "
            "approval, notices that just landed, and the documents you need to hand "
            "someone. Preparation and configuration stay on the web, where they "
            "belong."
        ),
        audience="Owners, directors, partners and anyone who approves",
        highlights=(
            ("Push for deadlines", "Not for everything. Notifications you keep on."),
            (
                "Step-up on approvals",
                "Approving on a phone re-authenticates exactly like approving on a laptop.",
            ),
            (
                "Sessions you control",
                "Every device is listed in your account and can be signed out "
                "remotely, taking effect immediately.",
            ),
            ("Works on a bad connection", "Built for a 4G signal in a factory office."),
        ),
    ),
    Module(
        slug="integrations",
        name="Integrations and API",
        nav_summary="Accounting, payroll, email, and a documented API.",
        icon="link",
        headline="It has to fit the systems you already run.",
        lede=(
            "STACOS is the compliance layer, not a replacement for your accounting or "
            "payroll system. It reads what it needs, writes back nothing it was not "
            "asked to, and exposes everything it knows through an API you can build "
            "against."
        ),
        audience="Finance teams, IT teams and firms with their own tooling",
        highlights=(
            (
                "Accounting and payroll",
                "Import entity, registration and period data instead of retyping it. "
                "Connectors are additive: nothing is written back without an explicit "
                "mapping.",
            ),
            (
                "Email intake",
                "A per-account intake address so a forwarded notice becomes a tracked record.",
            ),
            (
                "Webhooks",
                "Obligation status changes, notice intake and approvals, delivered to "
                "your endpoint with signed payloads.",
            ),
            (
                "A real API",
                "Documented, versioned, token-authenticated, and scoped by the same "
                "permission system the interface uses. Available on Enterprise.",
            ),
        ),
        faqs=(
            Faq(
                question="Do you connect directly to government portals?",
                answer=(
                    "Only where the authority provides a sanctioned interface and you "
                    "have authorised it. Where it does not, we do not scrape or store "
                    "portal passwords in the hope that it keeps working."
                ),
            ),
        ),
    ),
)


# ===========================================================================
# Audiences
# ===========================================================================

AUDIENCES: tuple[Audience, ...] = (
    Audience(
        slug="business",
        name="For businesses",
        nav_summary="Know what applies to you and never miss a date.",
        headline="You should not have to know the statute to comply with it.",
        lede=(
            "Most businesses do not miss deadlines because they are careless. They "
            "miss them because nobody holds a complete list of what applies, and the "
            "list changes when the business does — a new state, a new factory, a "
            "headcount crossing a threshold nobody was watching."
        ),
        pains=(
            (
                "The list lives in someone’s head",
                "Usually one person’s, and usually the person who is about to go on leave.",
            ),
            (
                "Your CA is on WhatsApp",
                "Documents scroll away. Approvals are “ok” in a chat. Nothing is "
                "reconstructable a year later.",
            ),
            (
                "Growth adds obligations silently",
                "Crossing a threshold or opening in a second state changes what you "
                "owe, and nothing tells you.",
            ),
            (
                "Diligence is a fire drill",
                "An acquirer or a lender asks for three years of filings and the "
                "company spends a fortnight in a shared drive.",
            ),
        ),
        outcomes=(
            (
                "A complete calendar on day one",
                "Generated from your profile, not assembled by you.",
            ),
            (
                "One place your CA also works in",
                "Scoped, revocable, and yours when the relationship ends.",
            ),
            (
                "Evidence attached to every claim",
                "Acknowledgements against obligations, not in an inbox.",
            ),
            (
                "A trail that answers questions",
                "Who approved what, when, and what it looked like before.",
            ),
        ),
        module_slugs=(
            "compliance-calendar",
            "notices",
            "documents",
            "collaboration",
            "audit-trail",
        ),
        plan_code="business",
        sections=(
            Section(
                heading="What the first week looks like",
                paragraphs=(
                    "Create the account, add your entity, and answer the profile. The "
                    "calendar generates. You review it with whoever knows the business "
                    "best and mark the handful of things that do not apply, with "
                    "reasons.",
                    "Then invite your CA or CS. Choose the entities and the categories "
                    "they should see, and they are in — with their own login, their "
                    "own audit rows, and no access to anything outside that scope.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="We already have a CA. Does this replace them?",
                answer=(
                    "No, and it is not meant to. It replaces the spreadsheet, the "
                    "chat thread and the shared drive between you and them. Firms "
                    "invited by clients tend to end up running their own practice on "
                    "it too."
                ),
            ),
            Faq(
                question="How long does setup take?",
                answer=(
                    "The profile is a short form. Most single-entity businesses have a "
                    "working calendar the same afternoon; multi-state groups take "
                    "longer because there are more registrations to record."
                ),
            ),
        ),
    ),
    Audience(
        slug="practice",
        name="For CA and CS firms",
        nav_summary="Every client, every deadline, and the economics of the firm.",
        headline="One board for the whole client book — and the numbers behind it.",
        lede=(
            "A practice does not fail on technical skill. It fails on visibility: "
            "which client is closest to breaching, who is overloaded this week, what "
            "was written off last quarter and why. STACOS puts the compliance work "
            "and the economics of doing it in the same system."
        ),
        pains=(
            (
                "Deadlines tracked per client",
                "Twenty spreadsheets is not a system; it is twenty single points of failure.",
            ),
            (
                "Chasing documents is the job",
                "Associates spend their week asking for the same three files.",
            ),
            (
                "Nobody knows the recovery rate",
                "Fees are quoted on instinct because realisation per client was never measured.",
            ),
            (
                "Handover loses everything",
                "When an associate leaves, the context leaves with them.",
            ),
        ),
        outcomes=(
            (
                "Portfolio risk board",
                "Every obligation across every client, ranked by proximity to breach.",
            ),
            (
                "Requests that chase themselves",
                "Raise once; it reminds, escalates and files the response.",
            ),
            (
                "Time to invoice to recovery",
                "Logged against a client and an obligation, all the way through.",
            ),
            (
                "Firm-wide method",
                "Checklists and review steps defined once, applied everywhere.",
            ),
        ),
        module_slugs=("practice", "compliance-calendar", "documents", "notices", "collaboration"),
        plan_code="practice",
        sections=(
            Section(
                heading="Clients you serve, clients who invited you",
                paragraphs=(
                    "Both work the same way. A client can hold their own account and "
                    "engage you on part of it; or you can hold the entity and give the "
                    "client a view. In either case the engagement defines the scope, "
                    "and neither side loses their record when it ends.",
                ),
            ),
            Section(
                heading="What the partners see",
                paragraphs=(
                    "Work in progress by client and by engagement, realisation against "
                    "quoted fees, write-offs with reasons, and capacity by person and "
                    "by week. Enough to decide what to take on and what to reprice.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="Can we white-label it for our clients?",
                answer=(
                    "Yes, as an add-on: your logo and your subdomain on the surfaces "
                    "your clients use."
                ),
            ),
            Faq(
                question="What happens to client data if we stop using STACOS?",
                answer=(
                    "You export it — records, documents and the audit trail — and the "
                    "client’s own account keeps their copy regardless of what your "
                    "firm does."
                ),
            ),
        ),
    ),
    Audience(
        slug="enterprise",
        name="For groups and enterprises",
        nav_summary="Many entities, many states, custom roles and SSO.",
        headline="Ten entities in six states is a different problem, not a bigger one.",
        lede=(
            "At group scale the question stops being “what is due” and becomes “who "
            "is allowed to see it, who signed off, and can we prove it”. That is a "
            "permissions, delegation and evidence problem — which is what this "
            "product is underneath."
        ),
        pains=(
            (
                "Per-entity silos",
                "Each subsidiary tracked separately, with no group view and no common vocabulary.",
            ),
            (
                "Delegation without records",
                "Approvals happen by email and cannot be reconstructed at audit.",
            ),
            (
                "Access sprawl",
                "Consultants added for one project, still in the shared drive two years later.",
            ),
            (
                "Internal audit asks the hard question",
                "“Show me every change to this filing.” Most systems cannot.",
            ),
        ),
        outcomes=(
            (
                "Group and entity views",
                "Roll up across the group; drill to one registration in one state.",
            ),
            (
                "Custom roles",
                "Built from permission codes and scoped by entity, department and "
                "compliance category.",
            ),
            (
                "SAML single sign-on",
                "Joiners and leavers handled by your identity provider.",
            ),
            (
                "Contractual assurance",
                "Data processing agreement, defined support targets and a named point of contact.",
            ),
        ),
        module_slugs=("audit-trail", "integrations", "registers", "compliance-calendar"),
        plan_code="enterprise",
        sections=(
            Section(
                heading="How access is actually constrained",
                paragraphs=(
                    "Every customer-owned row carries the account that owns it, and "
                    "the application’s default database manager refuses to run a query "
                    "that has not been scoped to an account. PostgreSQL row-level "
                    "security enforces the same boundary independently, using a "
                    "database role that cannot bypass it.",
                    "An adversarial test suite acts as one account and demands "
                    "another’s rows, against every model, on every build. If any "
                    "attempt succeeds, the build fails. The security page describes "
                    "this in the detail your procurement team will want.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="Can we run STACOS in our own environment?",
                answer=(
                    "Talk to us. The standard offering is managed, in an Indian "
                    "region; dedicated deployments are handled case by case under an "
                    "enterprise agreement."
                ),
            ),
        ),
    ),
)


# ===========================================================================
# Partners
# ===========================================================================

PARTNER_TRACKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Professional firms",
        "Chartered accountants, company secretaries, cost accountants and tax "
        "consultants who bring their client book onto STACOS.",
        (
            "Practice plan at partner terms",
            "Client seats included for entities you manage",
            "Joint onboarding for your first clients",
            "Named contact for escalations",
        ),
    ),
    (
        "Resellers and dealers",
        "Regional partners who sell and support STACOS under their own brand.",
        (
            "White-label surfaces: your logo, your subdomain",
            "Margin on subscription and add-ons",
            "Sales and product training",
            "Deal registration",
        ),
    ),
    (
        "Technology partners",
        "Accounting, payroll and ERP products whose customers need a compliance layer beside them.",
        (
            "Documented API and webhooks",
            "Co-built connectors",
            "Sandbox account for development",
            "Joint go-to-market where it fits",
        ),
    ),
)


# ===========================================================================
# Pricing
# ===========================================================================

PLANS: tuple[Plan, ...] = (
    Plan(
        code="starter",
        name="Starter",
        tagline="One entity, every due date, nothing missed.",
        monthly_inr=Decimal("999"),
        annual_inr=Decimal("9990"),
        included_entities=1,
        included_users=3,
        features=(
            "Automatic compliance calendar",
            "Due-date reminders by email and in-app",
            "Document vault with 5 GB storage",
            "Invite your CA or CS",
            "Mobile app",
        ),
    ),
    Plan(
        code="business",
        name="Business",
        tagline="Multiple entities, notices, and information requests.",
        monthly_inr=Decimal("3499"),
        annual_inr=Decimal("34990"),
        included_entities=3,
        included_users=10,
        features=(
            "Everything in Starter",
            "Up to 3 entities, multi-state",
            "Notice tracker with deadline countdown",
            "Information requests with reminders",
            "Statutory registers and minutes",
            "WhatsApp and SMS reminders",
            "Full audit trail export",
        ),
        highlight=True,
    ),
    Plan(
        code="practice",
        name="Practice",
        tagline="Run the whole firm: clients, work, time and billing.",
        monthly_inr=Decimal("7999"),
        annual_inr=Decimal("79990"),
        included_entities=25,
        included_users=15,
        features=(
            "Everything in Business",
            "Client book and portfolio risk board",
            "Work allocation and capacity heatmap",
            "Time tracking, WIP and billing",
            "Return preparation and maker-checker",
            "Practice-wide checklist templates",
            "Profitability by client and engagement",
        ),
    ),
    Plan(
        code="enterprise",
        name="Enterprise",
        tagline="Custom roles, SSO, API, and a data processing agreement.",
        monthly_inr=Decimal("0"),
        annual_inr=Decimal("0"),
        included_entities=0,
        included_users=0,
        features=(
            "Everything in Practice",
            "Unlimited entities and users",
            "Custom roles and department scoping",
            "SAML single sign-on",
            "API access and webhooks",
            "Data processing agreement and DPDP support",
            "Dedicated success manager",
        ),
        cta="Talk to sales",
    ),
)

ADD_ONS = (
    ("Additional entity", "₹399 / month", "Beyond your plan's included entities."),
    ("Additional user", "₹199 / month", "Beyond your plan's included seats."),
    ("Notice auto-retrieval", "₹999 / month", "Where the authority permits it."),
    ("WhatsApp reminders", "₹499 / month", "Per entity, DLT-registered templates."),
    ("White label", "₹4,999 / month", "Your logo and subdomain, for practices and dealers."),
)

COMPARISON_ROWS = (
    ("Compliance calendar", "1 entity", "3 entities", "25 entities", "Unlimited"),
    ("Users included", "3", "10", "15", "Unlimited"),
    ("Notice tracker", False, True, True, True),
    ("Information requests", False, True, True, True),
    ("Return preparation", False, False, True, True),
    ("Time tracking and billing", False, False, True, True),
    ("Custom roles", False, False, False, True),
    ("SAML single sign-on", False, False, False, True),
    ("API access", False, False, False, True),
)

PRICING_FAQS: tuple[Faq, ...] = (
    Faq(
        question="Is there a free trial?",
        answer=(
            "Fourteen days, no card required. The trial is the full product on one "
            "entity, including inviting your CA."
        ),
    ),
    Faq(
        question="What counts as an entity?",
        answer=(
            "One legal person — a company, an LLP, a partnership, a proprietorship or "
            "a trust. Multiple registrations, branches or factories under the same "
            "legal entity are included; they are not billed separately."
        ),
    ),
    Faq(
        question="Do you charge my CA for a seat?",
        answer=(
            "No. Users from a firm engaged on your entities work under their own "
            "firm’s subscription. Seats on your plan are for your own people."
        ),
    ),
    Faq(
        question="Is GST included in the price?",
        answer=(
            "Prices are shown exclusive of tax and GST is applied at checkout. A tax "
            "invoice with your GSTIN is issued for every payment."
        ),
    ),
    Faq(
        question="What happens if I cancel?",
        answer=(
            "Your subscription runs to the end of the paid term and does not renew. "
            "Export your data at any point during that period; see the cancellation "
            "and refund policy for the detail."
        ),
    ),
    Faq(
        question="Can I change plans mid-term?",
        answer=(
            "Upgrade at any time and pay the prorated difference. Downgrades take "
            "effect at the next renewal, so you never lose access to data you are "
            "still paying for."
        ),
    ),
)


# ===========================================================================
# Frequently asked questions (the standalone page)
# ===========================================================================

FAQ_SECTIONS: tuple[FaqSection, ...] = (
    FaqSection(
        title="The product",
        faqs=(
            Faq(
                question="What does STACOS actually do?",
                answer=(
                    "It works out which statutory obligations apply to your business, "
                    "generates the calendar with correct due dates, holds the evidence "
                    "for each one, tracks notices, and lets your CA or CS work in the "
                    "same place under scoped, revocable access — with an append-only "
                    "record of everything that happened."
                ),
            ),
            Faq(
                question="Does it file returns for me?",
                answer=(
                    "No. Filing happens on the relevant portal by the person "
                    "authorised to do it. STACOS prepares, tracks, reminds and holds "
                    "the acknowledgement as proof."
                ),
            ),
            Faq(
                question="Which obligations are covered?",
                answer=(
                    "Tax, corporate, payroll, labour and establishment obligations for "
                    "Indian entities, driven by a versioned rule set rather than a "
                    "fixed list in the code. Coverage grows by publishing rules, not "
                    "by shipping a release."
                ),
            ),
            Faq(
                question="Do you support businesses outside India?",
                answer=(
                    "The core is jurisdiction-neutral by design: no table carries an "
                    "India-specific column, and every rule is data. India is what is "
                    "published today."
                ),
            ),
        ),
    ),
    FaqSection(
        title="Access and collaboration",
        faqs=(
            Faq(
                question="How does my CA get access?",
                answer=(
                    "You invite their firm and approve an engagement scoped to named "
                    "entities and named compliance categories, with a start and end "
                    "date. That is the only route into your data."
                ),
            ),
            Faq(
                question="What happens when we change CA?",
                answer=(
                    "End the engagement. Access stops on the same request, and every "
                    "record, document and audit row stays with you."
                ),
            ),
            Faq(
                question="Can I stop someone internally from seeing everything?",
                answer=(
                    "Yes. Roles are built from permission codes and scoped by entity, "
                    "department and compliance category, so a payroll manager sees "
                    "payroll and not the tax notices."
                ),
            ),
        ),
    ),
    FaqSection(
        title="Security and data",
        faqs=(
            Faq(
                question="Where is my data stored?",
                answer="Data belonging to Indian customers is stored in an Indian region.",
            ),
            Faq(
                question="Can your staff see my data?",
                answer=(
                    "Not without your consent. Support access is requested, "
                    "time-boxed, shown to you while it is active, and written to your "
                    "audit trail."
                ),
            ),
            Faq(
                question="Can I export everything?",
                answer=(
                    "Yes — records, documents and the full audit trail — at any time, "
                    "and after cancellation for the duration of the retention window."
                ),
            ),
            Faq(
                question="Do you hold a security certification?",
                answer=(
                    "We publish what is true today rather than a badge: the security "
                    "page describes the isolation model, the authentication "
                    "requirements and the audit guarantees, and we will complete your "
                    "security questionnaire."
                ),
            ),
        ),
    ),
    FaqSection(
        title="Buying",
        faqs=PRICING_FAQS,
    ),
    FaqSection(
        title="Getting started",
        faqs=(
            Faq(
                question="How do I sign in?",
                answer=(
                    "With your email and password, plus a code sent to your email and "
                    "a code sent to your mobile the first time you use a device. "
                    "Google, Microsoft and Apple sign-in are available and do not "
                    "replace either code."
                ),
            ),
            Faq(
                question="Is there an implementation service?",
                answer=(
                    "For groups and practices, yes: profile setup, historical "
                    "document loading and role design, quoted per engagement. Smaller "
                    "businesses generally do not need it."
                ),
            ),
            Faq(
                question="Do you offer training?",
                answer=(
                    "Live onboarding sessions for Practice and Enterprise customers, "
                    "and short task-level guides in the product for everyone."
                ),
            ),
        ),
    ),
)


# ===========================================================================
# Guides — the pages someone lands on from a search
# ===========================================================================

GUIDES: tuple[Guide, ...] = (
    Guide(
        slug="what-a-compliance-calendar-should-contain",
        title="What a compliance calendar should contain",
        summary=(
            "Most compliance calendars are a list of dates. A list of dates cannot "
            "tell you whether you complied. Here is what a row needs to carry."
        ),
        published=date(2026, 3, 4),
        updated=date(2026, 7, 22),
        minutes=7,
        topic="Compliance operations",
        sections=(
            Section(
                heading="A date is not an obligation",
                paragraphs=(
                    "The spreadsheet version of a compliance calendar has two columns: "
                    "what is due and when. It survives contact with reality for about "
                    "one quarter, because the questions people actually ask of it are "
                    "different questions.",
                    "“Which state was that for?” “Who filed it?” “Where is the "
                    "acknowledgement?” “Was the date extended, and by which "
                    "notification?” “Why did we decide this does not apply to us?” "
                    "None of those are answerable from two columns.",
                ),
            ),
            Section(
                heading="The eight things a row has to carry",
                bullets=(
                    "The period it covers — not just the due date. A late filing for "
                    "an old period is a different fact from an upcoming one.",
                    "The registration or premises it belongs to, so multi-state "
                    "obligations do not collapse into one row.",
                    "The statutory date and, separately, any extended date.",
                    "A named owner inside the business.",
                    "A named preparer, where a professional firm is engaged.",
                    "A status drawn from a fixed vocabulary, so “done” means the same "
                    "thing to everyone.",
                    "The evidence: the return, the challan, the acknowledgement.",
                    "The history: who changed the row, when, and what it said before.",
                ),
            ),
            Section(
                heading="Applicability is the hard part",
                paragraphs=(
                    "Anyone can copy a list of due dates. The difficult question is "
                    "which of them apply to this business, and that depends on facts "
                    "that change: turnover crossing a threshold, headcount crossing "
                    "another, a second state, a factory licence, an import.",
                    "A calendar that is generated from those facts re-derives when they "
                    "change. A calendar that was typed once does not, and the failure "
                    "is silent — the new obligation simply never appears.",
                ),
            ),
            Section(
                heading="Exceptions must be recorded, not deleted",
                paragraphs=(
                    "When something genuinely does not apply, the honest record is “not "
                    "applicable, because X, decided by Y on Z” — not an absent row. "
                    "Absence is indistinguishable from an oversight, and two years "
                    "later nobody remembers which it was.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="How often should the calendar be reviewed?",
                answer=(
                    "Whenever a fact about the business changes, and once a quarter "
                    "regardless. Threshold crossings are the usual source of surprise."
                ),
            ),
        ),
        related_slugs=("evidence-that-survives-an-audit", "multi-state-compliance"),
    ),
    Guide(
        slug="evidence-that-survives-an-audit",
        title="Evidence that survives an audit",
        summary=(
            "The filing is the easy part. Being able to prove, years later, what was "
            "filed and who approved it is what an audit actually tests."
        ),
        published=date(2026, 4, 15),
        updated=date(2026, 6, 30),
        minutes=6,
        topic="Audit and assurance",
        sections=(
            Section(
                heading="The question is always “show me”",
                paragraphs=(
                    "Auditors, acquirers and regulators converge on the same request: "
                    "show me the filing, show me the proof it was accepted, show me who "
                    "approved it, and show me that the record has not been edited since.",
                    "Three of those four are about the trail, not the filing.",
                ),
            ),
            Section(
                heading="Attach evidence to the claim, not to a folder",
                paragraphs=(
                    "A shared drive organised by month can hold every acknowledgement "
                    "and still not answer which challan proves which period. Evidence "
                    "belongs against the obligation it evidences, so the claim and the "
                    "proof cannot drift apart when someone reorganises the folders.",
                ),
            ),
            Section(
                heading="Append-only beats well-intentioned",
                paragraphs=(
                    "A history that can be edited is a history that will be — usually "
                    "innocently, to “fix” a typo, and always at the worst moment for "
                    "credibility. The stronger guarantee is structural: a database that "
                    "rejects updates and deletes on the audit table, so nobody can "
                    "revise it, including the vendor.",
                    "That is a property you can demonstrate in a due-diligence meeting "
                    "in about thirty seconds, which is roughly how long anyone is "
                    "willing to spend on the question.",
                ),
            ),
            Section(
                heading="Approval is a record, not a message",
                paragraphs=(
                    "“Ok, go ahead” in a chat thread is not an approval anyone can "
                    "rely on later. An approval worth the name names the person, the "
                    "exact version they approved, the time, and the re-authentication "
                    "that proved it was really them.",
                ),
            ),
        ),
        related_slugs=("what-a-compliance-calendar-should-contain", "working-with-your-ca"),
    ),
    Guide(
        slug="multi-state-compliance",
        title="Multi-state compliance without multiplying the mistakes",
        summary=(
            "A second state does not double the work. It changes its shape: same "
            "obligation, several registrations, several owners, several deadlines."
        ),
        published=date(2026, 5, 6),
        updated=date(2026, 7, 8),
        minutes=8,
        topic="Growth",
        sections=(
            Section(
                heading="One row per registration, always",
                paragraphs=(
                    "The most common structural error in a compliance tracker is a "
                    "single row for an obligation that is actually owed once per "
                    "registration. It looks tidy and it hides the miss: the row is "
                    "marked complete when three of four states have filed.",
                ),
            ),
            Section(
                heading="Premises are not registrations",
                paragraphs=(
                    "Factories, warehouses and branch offices generate their own "
                    "obligations — licences, returns, displays, inspections — that do "
                    "not follow the tax registration structure. Modelling them "
                    "separately is what stops a plant licence from being nobody’s job.",
                ),
            ),
            Section(
                heading="Local ownership, central visibility",
                paragraphs=(
                    "The person who can actually walk to the inspector’s office is at "
                    "the plant. The person accountable for the group’s compliance is at "
                    "head office. Both need to see the same row, with the local owner "
                    "named on it.",
                ),
            ),
            Section(
                heading="Watch the thresholds, not the calendar",
                paragraphs=(
                    "Expansion changes applicability before it changes anyone’s "
                    "workload. Headcount, turnover, floor area and power load are the "
                    "usual triggers, and they are crossed by operations teams who have "
                    "no reason to think about statutes.",
                    "Recording these as facts about the entity — and re-deriving the "
                    "calendar when they change — turns a surprise into a notification.",
                ),
            ),
        ),
        related_slugs=("what-a-compliance-calendar-should-contain", "choosing-compliance-software"),
    ),
    Guide(
        slug="working-with-your-ca",
        title="Working with your CA without losing the thread",
        summary=(
            "The relationship is fine. The channel is the problem. What a shared "
            "workspace changes, and what it should never change."
        ),
        published=date(2026, 2, 11),
        updated=date(2026, 6, 3),
        minutes=6,
        topic="Collaboration",
        sections=(
            Section(
                heading="Why chat fails at this specific job",
                paragraphs=(
                    "Chat is excellent at conversation and terrible at record. "
                    "Documents scroll away, approvals are ambiguous, nobody can tell "
                    "which version was final, and access is all-or-nothing: your CA "
                    "either has the whole thread or none of it.",
                ),
            ),
            Section(
                heading="Scope the access, then forget about it",
                paragraphs=(
                    "The right model is an engagement: a named firm, named entities, "
                    "named categories, a start and an end. It answers “who at my firm "
                    "can see my data” without anyone having to remember, and ending it "
                    "is one action rather than an audit of shared folders.",
                ),
            ),
            Section(
                heading="Prepare and approve as separate acts",
                paragraphs=(
                    "The firm prepares; the business approves. Keeping those distinct — "
                    "with a re-authentication on the approval — protects both sides. "
                    "The client is never surprised by a filing, and the firm has proof "
                    "it was authorised.",
                ),
            ),
            Section(
                heading="Own your record",
                paragraphs=(
                    "Whatever else changes, the business should hold its own history. "
                    "If moving firms means losing three years of filings, the leverage "
                    "sits in the wrong place — and the person harmed by that is the "
                    "client, not the incumbent firm.",
                ),
            ),
        ),
        related_slugs=("evidence-that-survives-an-audit", "choosing-compliance-software"),
    ),
    Guide(
        slug="dpdp-and-your-compliance-data",
        title="The DPDP Act and the data your compliance system holds",
        summary=(
            "Compliance systems hold employee and director personal data by "
            "definition. What that means for consent, retention and rights."
        ),
        published=date(2026, 1, 28),
        updated=date(2026, 7, 15),
        minutes=9,
        topic="Privacy",
        sections=(
            Section(
                heading="You are a data fiduciary for this data",
                paragraphs=(
                    "Payroll registers, director records, employee documents and "
                    "identifiers are personal data. Holding them in a compliance system "
                    "does not change who is responsible for them: the business is the "
                    "fiduciary, and its software vendor is a processor acting on "
                    "instructions.",
                    "This is why a data processing agreement matters more than a "
                    "privacy badge. The agreement is what defines the instructions.",
                ),
            ),
            Section(
                heading="Purpose limitation is a schema question",
                paragraphs=(
                    "“We only use it for what it was collected for” is easy to write "
                    "and hard to prove. It becomes provable when the data is scoped by "
                    "purpose in the system itself — a payroll engagement that cannot "
                    "reach tax notices is purpose limitation you can demonstrate.",
                ),
            ),
            Section(
                heading="Retention has to be a schedule, not a habit",
                paragraphs=(
                    "Different classes of record have different statutory retention "
                    "periods, and “keep everything forever” is both a privacy problem "
                    "and a discovery problem. A retention class per document type, "
                    "applied automatically, is the only version of this that survives "
                    "contact with a busy team.",
                ),
            ),
            Section(
                heading="Rights requests need a route",
                paragraphs=(
                    "Access, correction and erasure requests arrive at the business, "
                    "not the vendor. What the business needs from its software is the "
                    "ability to find everything about one person quickly, to correct "
                    "it with a trail, and to erase what is not under a statutory "
                    "retention obligation.",
                ),
            ),
        ),
        faqs=(
            Faq(
                question="Does STACOS act as a fiduciary or a processor?",
                answer=(
                    "A processor, for the customer data you hold in your account. We "
                    "are a fiduciary only for the data of the people who deal with us "
                    "directly — your account users, and visitors to this website."
                ),
            ),
        ),
        related_slugs=("evidence-that-survives-an-audit",),
    ),
    Guide(
        slug="choosing-compliance-software",
        title="Choosing compliance software: eleven questions",
        summary=(
            "A short, hostile evaluation checklist. Ask these of us as readily as of anyone else."
        ),
        published=date(2026, 6, 17),
        updated=date(2026, 8, 1),
        minutes=7,
        topic="Buying",
        sections=(
            Section(
                heading="On coverage",
                bullets=(
                    "Is the obligation list generated from my business’s facts, or is "
                    "it a template I have to prune?",
                    "What happens to the calendar when I open in a new state — "
                    "automatic, or a support ticket?",
                    "When a due date is extended by notification, how quickly does it "
                    "change, and is the original date still visible?",
                ),
            ),
            Section(
                heading="On isolation",
                bullets=(
                    "What stops one customer’s query from returning another "
                    "customer’s rows? Ask for the mechanism, not the assurance.",
                    "Is there a second, independent enforcement layer at the "
                    "database, or only application code?",
                    "Can your staff read my data, and what would I see if they did?",
                ),
            ),
            Section(
                heading="On the record",
                bullets=(
                    "Can the audit history be edited or deleted by anyone, including the vendor?",
                    "Can I export everything — records, documents and the trail — without asking?",
                    "If I leave, what do I keep and for how long?",
                ),
            ),
            Section(
                heading="On the relationship",
                bullets=(
                    "Is my professional firm charged for access to my account?",
                    "What is the actual support route, and who answers it?",
                ),
            ),
            Section(
                heading="A note on how to read the answers",
                paragraphs=(
                    "The useful signal is specificity. “Enterprise-grade security” is "
                    "not an answer to any question above; “an unscoped query raises an "
                    "exception, and row-level security enforces the same boundary at "
                    "the database using a role that cannot bypass it” is. Whether the "
                    "vendor is us or someone else, insist on the second kind.",
                ),
            ),
        ),
        related_slugs=("what-a-compliance-calendar-should-contain", "working-with-your-ca"),
    ),
)


# ===========================================================================
# Changelog — public, because “what changed” is a compliance question
# ===========================================================================

CHANGELOG: tuple[tuple[date, str, tuple[str, ...]], ...] = (
    (
        date(2026, 8, 4),
        "Public site, in full",
        (
            "Product, solution, resource, company and legal pages published.",
            "Sitemap and robots directives served from the application.",
            "Contact route with a routed enquiry form.",
        ),
    ),
    (
        date(2026, 7, 21),
        "Engagement scoping hardened",
        (
            "Ending an engagement now revokes access within the same request rather "
            "than at the next scope refresh.",
            "Engagement scope is shown to both sides on the engagement record.",
        ),
    ),
    (
        date(2026, 7, 2),
        "Trusted devices and session control",
        (
            "Every active session and trusted device is listed in your account.",
            "Remote sign-out takes effect immediately, including on the mobile app.",
        ),
    ),
    (
        date(2026, 6, 10),
        "Audit trail export",
        (
            "Export the full trail for your account, with before-and-after values.",
            "Support access sessions appear in the trail as ordinary actors.",
        ),
    ),
    (
        date(2026, 5, 19),
        "Foundation",
        (
            "Tenancy, identity, dual-channel verification, roles and permissions.",
            "Row-level security enabled on every tenant-scoped table.",
        ),
    ),
)


# ===========================================================================
# Careers
# ===========================================================================

OPEN_ROLES: tuple[tuple[str, str, str, str], ...] = (
    (
        "Senior Django engineer",
        "Engineering",
        "Mumbai or remote (India)",
        "Own the compliance engine: rule evaluation, due-date computation, and the "
        "golden-file tests that keep both honest.",
    ),
    (
        "Compliance content lead (CA or CS)",
        "Content",
        "Mumbai or remote (India)",
        "Turn statutes and notifications into the versioned rule data the product "
        "runs on. Qualification and practice experience matter more than software "
        "experience.",
    ),
    (
        "Product designer",
        "Design",
        "Mumbai",
        "Dense, professional interfaces for people under deadline pressure. "
        "Accessibility is a requirement here, not a phase.",
    ),
    (
        "Customer success manager",
        "Customer",
        "Mumbai",
        "Onboard practices and groups: profile setup, role design, and the first "
        "quarter of real deadlines.",
    ),
)


# ===========================================================================
# Sub-processors — named, because procurement always asks
# ===========================================================================

SUBPROCESSORS: tuple[tuple[str, str, str, str], ...] = (
    ("Cloud hosting", "Application and database hosting", "India", "All customer data"),
    ("Object storage", "Document storage", "India", "Uploaded documents"),
    ("Transactional email", "Verification codes, reminders, notifications", "India", "Name, email"),
    ("SMS and WhatsApp", "Verification codes and reminders", "India", "Name, mobile number"),
    ("Payment gateway (domestic)", "Subscription payments in INR", "India", "Billing details"),
    (
        "Payment gateway (international)",
        "Subscription payments outside India",
        "United States",
        "Billing details",
    ),
    ("Error monitoring", "Application diagnostics", "India", "Technical metadata only"),
)


# ===========================================================================
# Legal
# ===========================================================================

LEGAL_DOCUMENTS: tuple[LegalDocument, ...] = (
    LegalDocument(
        slug="terms",
        title="Terms of service",
        summary=(
            "The agreement between you and us for the use of STACOS: what the "
            "service is, what each of us is responsible for, and how it ends."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/terms.html",
    ),
    LegalDocument(
        slug="privacy",
        title="Privacy policy",
        summary=(
            "What personal data we hold, why we hold it, who else sees it, how long "
            "we keep it, and what you can ask us to do about it."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/privacy.html",
    ),
    LegalDocument(
        slug="dpdp",
        title="DPDP notice",
        summary=(
            "Your rights as a data principal under the Digital Personal Data "
            "Protection Act, how to exercise them, and who to complain to."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/dpdp.html",
    ),
    LegalDocument(
        slug="cookies",
        title="Cookie policy",
        summary=(
            "Which cookies this website and the application set, what each one is "
            "for, and why there is no consent banner to click through."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/cookies.html",
    ),
    LegalDocument(
        slug="refunds",
        title="Cancellation and refunds",
        summary=(
            "How to cancel a subscription, what happens to your data afterwards, "
            "when we refund, when we do not, and how a refund is paid."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/refunds.html",
    ),
    LegalDocument(
        slug="acceptable-use",
        title="Acceptable use policy",
        summary=(
            "What you may not do with the service, why each restriction exists, and "
            "what happens if the policy is breached."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/acceptable_use.html",
    ),
    LegalDocument(
        slug="service-levels",
        title="Service levels and support",
        summary=(
            "Our availability target and service credits, planned maintenance "
            "practice, support response times, and backup and recovery objectives."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/service_levels.html",
    ),
    LegalDocument(
        slug="data-processing",
        title="Data processing addendum",
        summary=(
            "The processor terms that apply when we handle personal data in your "
            "account: scope, security measures, sub-processors, breach notification "
            "and deletion."
        ),
        effective=date(2026, 7, 1),
        template="marketing/legal/data_processing.html",
    ),
)


# ===========================================================================
# Navigation, derived
#
# The header and the footer are computed from the tuples above so that adding a
# module or a guide cannot leave the navigation stale.
# ===========================================================================

NAV: tuple[NavSection, ...] = (
    NavSection(
        label="Product",
        items=tuple(
            NavItem(
                label=module.name,
                url_name="marketing:module",
                args=(module.slug,),
                summary=module.nav_summary,
                icon=module.icon,
            )
            for module in MODULES
        ),
        footer_note="See the whole platform",
    ),
    NavSection(
        label="Solutions",
        items=(
            *(
                NavItem(
                    label=audience.name,
                    url_name="marketing:audience",
                    args=(audience.slug,),
                    summary=audience.nav_summary,
                )
                for audience in AUDIENCES
            ),
            NavItem(
                label="For partners",
                url_name="marketing:partners",
                summary="Resell, white-label or build on STACOS.",
            ),
        ),
    ),
    NavSection(
        label="Resources",
        items=(
            NavItem(
                label="Guides",
                url_name="marketing:resources",
                summary="Long-form writing on compliance operations.",
            ),
            NavItem(
                label="Frequently asked questions",
                url_name="marketing:faq",
                summary="Straight answers to the usual questions.",
            ),
            NavItem(
                label="Security",
                url_name="marketing:security",
                summary="Isolation, authentication and the audit guarantee.",
            ),
            NavItem(
                label="What’s new",
                url_name="marketing:changelog",
                summary="What shipped, and when.",
            ),
        ),
    ),
    NavSection(
        label="Company",
        items=(
            NavItem(
                label="About",
                url_name="marketing:about",
                summary="Why this product exists.",
            ),
            NavItem(
                label="Careers",
                url_name="marketing:careers",
                summary="Open roles.",
            ),
            NavItem(
                label="Contact",
                url_name="marketing:contact",
                summary="Sales, support, security and press.",
            ),
        ),
    ),
)


FOOTER: tuple[NavSection, ...] = (
    NavSection(
        label="Product",
        items=(
            NavItem(label="Platform overview", url_name="marketing:product"),
            *(NavItem(label=m.name, url_name="marketing:module", args=(m.slug,)) for m in MODULES),
            NavItem(label="Pricing", url_name="marketing:pricing"),
        ),
    ),
    NavSection(
        label="Solutions",
        items=(
            *(
                NavItem(label=a.name, url_name="marketing:audience", args=(a.slug,))
                for a in AUDIENCES
            ),
            NavItem(label="Partners", url_name="marketing:partners"),
        ),
    ),
    NavSection(
        label="Resources",
        items=(
            NavItem(label="Guides", url_name="marketing:resources"),
            NavItem(label="Frequently asked questions", url_name="marketing:faq"),
            NavItem(label="Security", url_name="marketing:security"),
            NavItem(label="Sub-processors", url_name="marketing:subprocessors"),
            NavItem(label="What’s new", url_name="marketing:changelog"),
        ),
    ),
    NavSection(
        label="Company",
        items=(
            NavItem(label="About", url_name="marketing:about"),
            NavItem(label="Careers", url_name="marketing:careers"),
            NavItem(label="Contact", url_name="marketing:contact"),
            NavItem(label="Sign in", url_name="accounts:login"),
        ),
    ),
    NavSection(
        label="Legal",
        items=tuple(
            NavItem(label=doc.title, url_name="marketing:legal", args=(doc.slug,))
            for doc in LEGAL_DOCUMENTS
            if doc.footer
        ),
    ),
)


# ===========================================================================
# Lookups
# ===========================================================================


def module_by_slug(slug: str) -> Module | None:
    return next((m for m in MODULES if m.slug == slug), None)


def audience_by_slug(slug: str) -> Audience | None:
    return next((a for a in AUDIENCES if a.slug == slug), None)


def guide_by_slug(slug: str) -> Guide | None:
    return next((g for g in GUIDES if g.slug == slug), None)


def legal_by_slug(slug: str) -> LegalDocument | None:
    return next((d for d in LEGAL_DOCUMENTS if d.slug == slug), None)


def plan_by_code(code: str) -> Plan | None:
    return next((p for p in PLANS if p.code == code), None)
