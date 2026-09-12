"""
The onboarding wizard.

Three steps and a commit, each step a page in its own right so the back button
and a bookmarked URL both work, and each swapping fragments as the user answers
so the preview keeps up without a reload.

Every view carries ``tenancy.onboarding.start``. That permission is granted by
the scope resolver to a user with no membership at all — this is the one flow in
the product that has to work before a tenant exists, and the reason
``entity_create`` cannot serve it is that it opens with ``raise Http404`` in
exactly that situation. It is deliberately *not* granted to somebody whose
membership was suspended; see ``tenancy.middleware``.

**Both render paths, on every step.** These three views used to answer every
request with the whole document, HTMX or not. Reached by a boosted click — from
the tenant switcher, from the setup redirect, or from the wizard's own Back link
— that document was morphed into ``#main``, giving the page a second
``.app-shell`` inside the first: two sidebars, two ``#main`` elements, two
``#toast-stack``s, two step rails showing 1-2-3 apiece, and a re-executed
``Alpine.start()``. The reported symptoms were "blank page", "sidebar stops
responding" and "the steps are numbered twice", and all three are that one
mistake. ``tests/test_htmx_navigation.py`` sweeps every app route for it.
"""

from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, derive_fragment_template, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.jurisdictions import subdivisions
from stacos.jurisdictions.decode import IdentityReport, read, sniff
from stacos.tenancy.onboarding.forms import IdentityForm, ProfileForm, QuestionForm
from stacos.tenancy.onboarding.services import (
    commit_draft,
    preview_draft,
    suggest_packs,
    total_obligation_count,
)
from stacos.tenancy.onboarding.state import DraftRegistration, OnboardingDraft
from stacos.tenancy.scope_resolver import SESSION_TENANT_KEY

PERMISSION = "tenancy.onboarding.start"

STEPS = (
    ("identity", _("Identify")),
    ("profile", _("Profile")),
    ("preview", _("Preview")),
)


def _today() -> date:
    return timezone.localdate()


def _template(request: HttpRequest, page: str) -> str:
    """The page, or just its body, depending on who is asking.

    ``obligations/list.html`` -> ``obligations/_fragments/list_body.html``. The
    same idiom every other app in the project uses; see ``stacos.core.htmx``.
    """
    return derive_fragment_template(page) if is_fragment_request(request) else page


def _step_context(step: str, draft: OnboardingDraft) -> dict[str, object]:
    return {"steps": STEPS, "current_step": step, "draft": draft}


# ---------------------------------------------------------------------------
# Step 1 — identify
# ---------------------------------------------------------------------------


@require_permission(PERMISSION)
@require_http_methods(["GET", "POST"])
def identity(request: HttpRequest) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)

    # "Create a new organisation" starts over deliberately, rather than
    # resuming whatever draft happens to be sitting in the session — carrying
    # over a half-finished "add a business" attempt into a "new organisation"
    # run would mix the two intents together. See `finish` for the other half
    # of this: it is what stops the new entity being folded into the tenant
    # the user is already signed into.
    if request.method == "GET" and request.GET.get("new_org"):
        draft = OnboardingDraft(new_organisation=True)
        draft.save(request.session)

    form = IdentityForm(request.POST or None, initial=draft.identifiers)

    if request.method == "POST" and form.is_valid():
        pairs = _identifier_pairs(form)
        report = read(pairs)
        prefill = report.prefill()

        draft = draft.with_(
            identifiers=dict(pairs),
            entity_type=prefill.get("entity_type", draft.entity_type),
            registered_office_state=prefill.get(
                "registered_office_state", draft.registered_office_state
            ),
            registrations=tuple(
                DraftRegistration(
                    type=kind,
                    value=value,
                    jurisdiction=prefill.get("jurisdiction", "") if kind == "GST" else "",
                )
                for kind, value in pairs
            ),
            states_of_operation=tuple(sorted({*draft.states_of_operation, *_states_from(report)})),
        )
        draft.save(request.session)
        return redirect("onboarding:profile")

    return render(
        request,
        _template(request, "tenancy/onboarding/identity.html"),
        {**_step_context("identity", draft), "form": form},
    )


@require_permission(PERMISSION)
@require_http_methods(["POST"])
def identity_decode(request: HttpRequest) -> HttpResponse:
    """Live decode as the user types. Reads nothing, writes nothing."""
    form = IdentityForm(request.POST)
    report = read(_identifier_pairs(form) if form.is_valid() else [])
    return render(
        request,
        "tenancy/onboarding/_fragments/identity_hints.html",
        {"report": report},
    )


def _identifier_pairs(form: IdentityForm) -> list[tuple[str, str]]:
    """Typed fields plus anything recognisable in the pasted blob."""
    if not form.is_valid():
        return []
    pairs = dict(sniff(form.cleaned_data.get("pasted", "")))
    # A typed field wins over the paste: it is the more deliberate answer.
    pairs.update(dict(form.identifiers()))
    return sorted(pairs.items())


def _states_from(report: IdentityReport) -> set[str]:
    return {
        value
        for hint in report.hints
        if hint.field in {"jurisdiction", "states_of_operation"}
        for value in hint.values
        if subdivisions.by_code(value) is not None
    }


# ---------------------------------------------------------------------------
# Step 2 — profile and the ranked questions
# ---------------------------------------------------------------------------


@require_permission(PERMISSION)
@require_http_methods(["GET", "POST"])
def profile(request: HttpRequest) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)
    report = read(sorted(draft.identifiers.items())) if draft.identifiers else None

    initial = {
        "name": draft.name,
        "legal_name": draft.legal_name,
        "entity_type": draft.entity_type,
        "registered_office_state": draft.registered_office_state,
        "incorporation_date": draft.incorporation_date or None,
        "states_of_operation": list(draft.states_of_operation),
    }
    form = ProfileForm(request.POST or None, initial=initial, report=report)

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        draft = draft.with_(
            name=data["name"],
            legal_name=data["legal_name"],
            entity_type=data["entity_type"],
            registered_office_state=data["registered_office_state"],
            incorporation_date=(
                data["incorporation_date"].isoformat() if data["incorporation_date"] else ""
            ),
            states_of_operation=tuple(sorted(set(data["states_of_operation"]))),
        )
        draft.save(request.session)
        return redirect("onboarding:preview")

    return render(
        request,
        _template(request, "tenancy/onboarding/profile.html"),
        {**_step_context("profile", draft), "form": form, "report": report},
    )


@require_permission(PERMISSION)
@require_http_methods(["POST"])
def answer_question(request: HttpRequest, fact_key: str) -> HttpResponse:
    """Record one answer and re-rank what to ask next.

    Re-ranking on every answer costs one evaluation of the catalog, well under a
    millisecond, and it is what makes the queue shrink faster than the user
    expects: answering "yes, GST registered" settles thirty definitions at once
    and the remaining questions reorder around what is left.
    """
    draft = OnboardingDraft.from_session(request.session)
    form = QuestionForm(request.POST, fact_key=fact_key)

    toast = None
    if form.is_valid():
        answers = dict(draft.answers)
        answer = form.answer()
        if answer is None:
            answers.pop(fact_key, None)
        else:
            answers[fact_key] = answer
        draft = draft.with_(answers=answers)
        draft.save(request.session)
    else:
        # An invalid answer used to be discarded in silence — the field just
        # reverted on the next render with no explanation, which reads as the
        # page ignoring what was typed rather than as a rejected value.
        message = next(iter(form.errors.get("answer", ())), _("That answer could not be saved."))
        toast = Toast(str(message), level="danger")

    return _render_preview_fragments(request, draft, toast=toast)


# ---------------------------------------------------------------------------
# Step 3 — the live preview
# ---------------------------------------------------------------------------


@require_permission(PERMISSION)
def preview(request: HttpRequest) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)
    if not draft.entity_type:
        return redirect("onboarding:profile")

    result = preview_draft(draft)
    packs = suggest_packs(draft, result)
    return render(
        request,
        _template(request, "tenancy/onboarding/preview.html"),
        {
            **_step_context("preview", draft),
            "preview": result,
            "packs": packs,
            "total_count": total_obligation_count(result, packs),
        },
    )


@require_permission(PERMISSION)
@require_http_methods(["POST"])
def toggle_pack(request: HttpRequest, code: str) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)
    packs = set(draft.packs)
    packs.symmetric_difference_update({code})
    draft = draft.with_(packs=tuple(sorted(packs)))
    draft.save(request.session)

    added = code in packs
    return _render_preview_fragments(
        request,
        draft,
        toast=Toast(_("Pack added.") if added else _("Pack removed.")),
    )


def _render_preview_fragments(
    request: HttpRequest, draft: OnboardingDraft, *, toast: Toast | None
) -> HttpResponse:
    """Re-render the three columns, the questions and the pack strip together.

    One response rather than three requests, because they are three views of one
    computation and letting them arrive separately would show the user a
    momentarily inconsistent screen.

    ``swap="innerHTML"`` on both out-of-band fragments, and ``hx-swap="innerHTML"``
    on the buttons that trigger this. The regions are declared once in
    ``preview_body.html`` and carry attributes the fragments do not know about —
    ``.wizard__aside``, and the ``aria-live`` that is the only reason a screen
    reader notices any of this changed. Replacing them outright discards both.

    The fragments no longer wrap themselves in ``<c-oob>`` either. They were
    doing that *and* being wrapped again by ``oob()``, inside a region already
    carrying the same id — three duplicate id pairs on first paint, and a fresh
    level of nesting on every answer.
    """
    result = preview_draft(draft)
    packs = suggest_packs(draft, result)
    context = {
        "preview": result,
        "packs": packs,
        "draft": draft,
        "total_count": total_obligation_count(result, packs),
    }
    return oob(
        request,
        Fragment("tenancy/onboarding/_fragments/preview_columns.html", context),
        also=[
            Fragment(
                "tenancy/onboarding/_fragments/question_queue.html",
                context,
                oob_target="onboarding-questions",
                swap="innerHTML",
            ),
            Fragment(
                "tenancy/onboarding/_fragments/pack_strip.html",
                context,
                oob_target="onboarding-packs",
                swap="innerHTML",
            ),
            Fragment(
                "tenancy/onboarding/_fragments/finish_button.html",
                context,
                oob_target="onboarding-finish-button",
            ),
        ],
        toast=toast,
    )


# ---------------------------------------------------------------------------
# Commit
#
# Not a fourth step in the rail: there is nothing to look at and nothing to
# answer, only the button at the bottom of the preview.
# ---------------------------------------------------------------------------


@require_permission(PERMISSION)
@require_http_methods(["POST"])
def finish(request: HttpRequest) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)
    if not draft.is_ready_to_commit:
        return redirect("onboarding:profile")

    # An organisation the user already owns is the one this entity belongs in
    # — unless the wizard was entered through "Create a new organisation",
    # which sets `draft.new_organisation` precisely so this branch can be
    # skipped. Without the flag, a signed-in user always has a `request.tenant`
    # bound, so every second run of the wizard would fold into it silently —
    # the correct move for "add a business", but not for somebody deliberately
    # starting a second, separate organisation.
    tenant = None if draft.new_organisation else getattr(request, "tenant", None)
    try:
        entity = commit_draft(
            draft,
            user=current_user(request),
            as_of=_today(),
            tenant=tenant,
        )
    except ValidationError as exc:
        # A mistyped GSTIN or CIN passes the identity step's own check — which
        # only confirms the shape — and fails a real rule (a checksum, a
        # cross-field constraint) only here, at commit. Without this, that
        # was an unhandled 500 with nothing on screen to say which of the ten
        # things the user typed was the problem.
        message = " ".join(exc.messages)
        if getattr(request, "htmx", False):
            return oob(request, toast=Toast(message, level="danger"), status=204)
        messages.error(request, message)
        return redirect("onboarding:preview")

    # Switch the session to the tenant just created, so the next page renders
    # inside it rather than falling back to the no-membership scope.
    request.session[SESSION_TENANT_KEY] = str(entity.tenant_id)
    request.session.pop("stacos_onboarding_draft", None)
    request.session.modified = True

    # Straight to the calendar, not the dashboard. The user has just answered ten
    # questions; the answer is what they came for.
    destination = f"{reverse('compliance:calendar')}?entity={entity.pk}"
    response = HttpResponse(status=204)
    response["HX-Redirect"] = destination
    if not getattr(request, "htmx", False):
        return redirect(destination)
    return response
