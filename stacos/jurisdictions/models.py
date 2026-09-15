"""
Jurisdiction packs: everything that varies by country, held as data.

The rule from the brief is absolute — nothing about India may be hardcoded. The
financial year starting in April, the Sunday weekend, the lakh/crore grouping,
the set of entity types, the tax identifiers and their validators: all of it is a
row here, so shipping the UAE means adding a pack rather than editing code.

Weekend definitions are **effective-dated**, not constant. The UAE moved the
public-sector weekend from Friday–Saturday to Saturday–Sunday in 2022; a
constant would silently produce wrong due dates for every date before the change.
"""

from __future__ import annotations

from django.db import models

from stacos.core.ids import uuid7
from stacos.core.models import TimeStampedModel

__all__ = [
    "Authority",
    "FactDefinition",
    "Holiday",
    "HolidayCalendar",
    "JurisdictionPack",
    "WeekendRule",
]


class JurisdictionPack(TimeStampedModel):
    """One country's conventions. Platform-owned reference data."""

    class DigitGrouping(models.TextChoices):
        INDIAN = "INDIAN", "Indian (1,23,45,678)"
        WESTERN = "WESTERN", "Western (12,345,678)"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    country = models.CharField(max_length=2, unique=True, help_text="ISO-3166-1 alpha-2.")
    name = models.CharField(max_length=100)
    currency = models.CharField(max_length=3, help_text="ISO-4217.")
    currency_symbol = models.CharField(max_length=8, default="")
    default_timezone = models.CharField(max_length=64, default="UTC")
    default_locale = models.CharField(max_length=12, default="en")
    digit_grouping = models.CharField(
        max_length=10, choices=DigitGrouping.choices, default=DigitGrouping.WESTERN
    )

    # Financial year convention. India is 4/1 (April–March); the UK is 4/6; the
    # UAE, Singapore and the US are 1/1. Never assumed anywhere in code.
    fy_start_month = models.PositiveSmallIntegerField(default=1)
    fy_start_day = models.PositiveSmallIntegerField(default=1)
    fy_label_template = models.CharField(
        max_length=60,
        default="FY {start_year}",
        help_text="e.g. 'FY {start_year}-{end_year_short}' renders as FY 2026-27.",
    )

    #: Sub-jurisdiction codes (states, emirates, provinces) this pack recognises.
    sub_jurisdictions = models.JSONField(default=list, blank=True)
    #: Entity types legally available in this country.
    entity_types = models.JSONField(default=list, blank=True)
    #: Registration/tax-identifier types, keyed to validators in `validators.py`.
    #: Shape: ``{"catalog": [...], "by_entity_type": {ENTITY_TYPE: [...]}}`` —
    #: see `stacos.jurisdictions.registration_requirements`.
    registration_types = models.JSONField(default=dict, blank=True)

    is_published = models.BooleanField(default=False)
    #: Community and partner packs carry attribution; see the brief's
    #: multi-country extensibility requirement.
    contributed_by = models.CharField(max_length=200, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["country"]

    def __str__(self) -> str:
        return f"{self.name} ({self.country})"


class WeekendRule(models.Model):
    """Which days are non-working, over a validity window.

    Effective-dated because weekends genuinely change: the UAE moved to
    Saturday–Sunday for the public sector in January 2022.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    pack = models.ForeignKey(
        JurisdictionPack, on_delete=models.CASCADE, related_name="weekend_rules"
    )
    #: Python weekday integers, Monday=0 … Sunday=6.
    weekend_days = models.JSONField(default=list)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["pack", "valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="weekendrule_window_sane",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.pack.country} weekend from {self.valid_from}"


class HolidayCalendar(TimeStampedModel):
    """A named set of non-working days.

    Calendars are per country *and* per sub-jurisdiction, because Indian state
    holidays differ substantially, and some obligations follow a bank calendar
    rather than a government one.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    key = models.CharField(max_length=32, unique=True, help_text="e.g. IN-NATIONAL, IN-GJ.")
    name = models.CharField(max_length=120)
    pack = models.ForeignKey(JurisdictionPack, on_delete=models.CASCADE, related_name="calendars")
    sub_jurisdiction = models.CharField(max_length=12, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"


class Holiday(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    calendar = models.ForeignKey(HolidayCalendar, on_delete=models.CASCADE, related_name="holidays")
    date = models.DateField(db_index=True)
    name = models.CharField(max_length=120)
    is_restricted = models.BooleanField(
        default=False, help_text="Optional holiday — offices may or may not close."
    )

    objects = models.Manager()

    class Meta:
        ordering = ["calendar", "date"]
        constraints = [
            models.UniqueConstraint(fields=["calendar", "date"], name="holiday_calendar_date_uniq"),
        ]
        indexes = [models.Index(fields=["calendar", "date"], name="holiday_cal_date_idx")]

    def __str__(self) -> str:
        return f"{self.date} {self.name}"


class Authority(TimeStampedModel):
    """A regulator obligations are filed with. Referenced by the catalog later."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    code = models.CharField(max_length=32, unique=True, help_text="e.g. CBIC, CBDT, MCA, EPFO.")
    name = models.CharField(max_length=200)
    short_name = models.CharField(max_length=60, blank=True)
    pack = models.ForeignKey(JurisdictionPack, on_delete=models.CASCADE, related_name="authorities")
    sub_jurisdiction = models.CharField(max_length=12, blank=True)
    website = models.URLField(blank=True)
    portal_url = models.URLField(blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "authorities"

    def __str__(self) -> str:
        return f"{self.short_name or self.name} ({self.code})"


class FactDefinition(TimeStampedModel):
    """Database projection of :data:`stacos.jurisdictions.facts.REGISTRY`.

    The registry lives in code — derived facts are functions, and they belong
    with the code that computes them. This table is a *projection*, refreshed by
    ``manage.py syncfacts``, so the future platform-admin rule editor can render
    a picker without importing Python, and so a fact can be documented in the
    admin. A test asserts the two never drift.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    key = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=200)
    fact_type = models.CharField(max_length=16)
    source = models.CharField(max_length=16)
    unit = models.CharField(max_length=16, blank=True)
    allowed_values = models.JSONField(default=list, blank=True)
    nullable = models.BooleanField(default=True)
    effective_dated = models.BooleanField(default=False)
    depends_on = models.JSONField(default=list, blank=True)
    help_text = models.TextField(blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return self.key
