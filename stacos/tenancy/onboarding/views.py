"""
The onboarding wizard.

Four steps, each a page in its own right so the back button and a bookmarked URL
both work, and each swapping fragments as the user answers so the preview keeps
up without a reload.

Every view carries ``tenancy.onboarding.start``. That permission is granted by
the scope resolver to a user with no membership at all — this is the one flow in
the product that has to work before a tenant exists, and the reason
``entity_create`` cannot serve it is that it opens with ``raise Http404`` in
exactly that situation.
"""

from __future__ import annotations

from datetime import date

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.jurisdictions import subdivisions
from stacos.jurisdictions.decode import IdentityReport, read, sniff
from stacos.tenancy.onboarding.forms import IdentityForm, ProfileForm, QuestionForm
from stacos.tenancy.onboarding.services import commit_draft, preview_draft, suggest_packs
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


def _step_context(step: str, draft: OnboardingDraft) -> dict[str, object]:
    return {"steps": STEPS, "current_step": step, "draft": draft}


# ---------------------------------------------------------------------------
# Step 1 — identify
# ---------------------------------------------------------------------------


@require_permission(PERMISSION)
@require_http_methods(["GET", "POST"])
def identity(request: HttpRequest) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)
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
        "tenancy/onboarding/identity.html",
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
        "tenancy/onboarding/profile.html",
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

    if form.is_valid():
        answers = dict(draft.answers)
        answer = form.answer()
        if answer is None:
            answers.pop(fact_key, None)
        else:
            answers[fact_key] = answer
        draft = draft.with_(answers=answers)
        draft.save(request.session)

    return _render_preview_fragments(request, draft, toast=None)


# ---------------------------------------------------------------------------
# Step 3 — the live preview
# ---------------------------------------------------------------------------


@require_permission(PERMISSION)
def preview(request: HttpRequest) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)
    if not draft.entity_type:
        return redirect("onboarding:profile")

    result = preview_draft(draft)
    return render(
        request,
        "tenancy/onboarding/preview.html",
        {
            **_step_context("preview", draft),
            "preview": result,
            "packs": suggest_packs(draft, result),
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
    """
    result = preview_draft(draft)
    context = {
        "preview": result,
        "packs": suggest_packs(draft, result),
        "draft": draft,
    }
    return oob(
        request,
        Fragment("tenancy/onboarding/_fragments/preview_columns.html", context),
        also=[
            Fragment(
                "tenancy/onboarding/_fragments/question_queue.html",
                context,
                oob_target="onboarding-questions",
            ),
            Fragment(
                "tenancy/onboarding/_fragments/pack_strip.html",
                context,
                oob_target="onboarding-packs",
            ),
        ],
        toast=toast,
    )


# ---------------------------------------------------------------------------
# Step 4 — commit
# ---------------------------------------------------------------------------


@require_permission(PERMISSION)
@require_http_methods(["POST"])
def finish(request: HttpRequest) -> HttpResponse:
    draft = OnboardingDraft.from_session(request.session)
    if not draft.is_ready_to_commit:
        return redirect("onboarding:profile")

    entity = commit_draft(draft, user=current_user(request), as_of=_today())

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
