"""Validation on the register's plain forms — no database, no fixtures."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from stacos.obligations.forms import FilingPendingForm


def test_a_past_expected_completion_date_is_rejected() -> None:
    """ "When do you expect it done?" answered with a date already behind us
    is not an estimate, it is a typo — reject it rather than recording a
    pending filing that is already overdue by its own answer."""
    yesterday = timezone.localdate() - timedelta(days=1)
    form = FilingPendingForm(
        {"pending_reason": "Waiting on the auditor", "expected_completion_date": yesterday}
    )

    assert not form.is_valid()
    assert "expected_completion_date" in form.errors


def test_today_is_an_acceptable_expected_completion_date() -> None:
    form = FilingPendingForm(
        {"pending_reason": "Filing today", "expected_completion_date": timezone.localdate()}
    )

    assert form.is_valid(), form.errors


def test_the_widget_advertises_today_as_the_earliest_pick() -> None:
    form = FilingPendingForm()
    assert form.fields["expected_completion_date"].widget.attrs["min"] == (
        timezone.localdate().isoformat()
    )
