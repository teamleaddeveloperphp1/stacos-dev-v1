"""
What an engagement can hand a firm, and what it never can.

An engagement is a client letting a firm work on some of its entities. It caps
what the firm's people may do there — it never adds to their role — and there
are things no engagement grants at all, whatever its ``permissions`` list says:

* **Signing off as the client.** ``compliance.obligation.approve`` is the
  business taking responsibility for what is filed in its name. A firm that
  could hold it would be approving its own work, which is the one thing a
  maker-checker chain exists to prevent.
* **The client's own administration** — its users, roles, billing, engagements,
  registrations, organisation settings, audit export. An engagement reaches
  entities' compliance records, not the business that owns them.

Django-free, so the list can be read by the resolver, the model's validation and
the roles guide without importing the ORM.
"""

from __future__ import annotations

__all__ = [
    "ENGAGEMENT_GRANTABLE",
    "ENGAGEMENT_READ_FLOOR",
    "PREPARE_REVIEW_AND_FILE",
    "engagement_ceiling",
]

#: What any live engagement lets a firm read on the entities it covers. An
#: engagement that let the firm see nothing would not be an engagement.
ENGAGEMENT_READ_FLOOR: frozenset[str] = frozenset(
    {
        "core.search",
        "tenancy.entity.view",
        "tenancy.profile.view",
        "compliance.obligation.view",
        "compliance.library.view",
        "catalog.view",
    }
)

#: Everything an engagement may grant on top of the floor. Anything else on an
#: engagement's list is ignored when access is resolved.
ENGAGEMENT_GRANTABLE: frozenset[str] = ENGAGEMENT_READ_FLOOR | {
    "finance.view",
    "tenancy.entity.edit",
    "tenancy.profile.edit",
    "tenancy.premises.manage",
    "tenancy.registration.view",
    "compliance.obligation.comment",
    "compliance.obligation.prepare",
    "compliance.obligation.request_info",
    "compliance.obligation.assign",
    "compliance.obligation.review",
    "compliance.obligation.file",
    "compliance.obligation.complete_unreviewed",
    "compliance.obligation.close",
    "compliance.obligation.reopen",
    "compliance.obligation.defer",
    "compliance.obligation.dismiss",
    "compliance.obligation.dispute",
    "compliance.event.record",
    "compliance.event.withdraw",
    "compliance.calendar.rebuild",
    "compliance.library.manage",
}


#: The ordinary engagement a business gives its CA: prepare, review and file,
#: without letting the firm skip review, reopen a closed filing, or rule an
#: obligation out. Used by the demo data; a client chooses its own.
PREPARE_REVIEW_AND_FILE: frozenset[str] = frozenset(
    {
        "tenancy.registration.view",
        "compliance.obligation.comment",
        "compliance.obligation.prepare",
        "compliance.obligation.request_info",
        "compliance.obligation.assign",
        "compliance.obligation.review",
        "compliance.obligation.file",
        "compliance.obligation.close",
        "compliance.obligation.defer",
        "compliance.obligation.dispute",
        "compliance.event.record",
    }
)


def engagement_ceiling(granted: frozenset[str] | set[str] | list[str]) -> frozenset[str]:
    """The most a firm member may do on an engaged entity, before their role.

    Not closed over ``implies`` — the caller expands it with the registry — so
    that this module stays importable without Django.
    """
    return ENGAGEMENT_READ_FLOOR | (frozenset(granted) & ENGAGEMENT_GRANTABLE)
