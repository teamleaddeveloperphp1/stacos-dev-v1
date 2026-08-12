"""
Load a jurisdiction pack from YAML.

Same reasoning as the compliance catalog: this is reference data that changes
(a state splits, a regulator is renamed, next year's holidays are gazetted) and
it belongs in git where a change is reviewable, not in a data migration that
cannot be corrected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stacos.jurisdictions.models import (
    Authority,
    Holiday,
    HolidayCalendar,
    JurisdictionPack,
    WeekendRule,
)

PACK_ROOT = Path(__file__).resolve().parents[4] / "catalog" / "packs"


class Command(BaseCommand):
    help = "Load jurisdiction packs (fiscal year, weekends, holidays, authorities) from YAML."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "country",
            nargs="?",
            default=None,
            help="ISO-3166-1 alpha-2 code. Omit to load every pack.",
        )
        parser.add_argument("--root", type=Path, default=None)

    def handle(self, *args: Any, **options: Any) -> None:
        root = options["root"] or PACK_ROOT
        if not root.exists():
            raise CommandError(f"No pack directory at {root}")

        country = options["country"]
        paths = [root / f"{country.upper()}.yaml"] if country else sorted(root.glob("*.yaml"))

        for path in paths:
            if not path.exists():
                raise CommandError(f"No pack file at {path}")
            with path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
            self._load(raw, source=path)

    @transaction.atomic
    def _load(self, raw: dict[str, Any], *, source: Path) -> None:
        pack, created = JurisdictionPack.objects.update_or_create(
            country=raw["country"],
            defaults={
                "name": raw["name"],
                "currency": raw["currency"],
                "currency_symbol": raw.get("currency_symbol", ""),
                "default_timezone": raw.get("default_timezone", "UTC"),
                "default_locale": raw.get("default_locale", "en"),
                "digit_grouping": raw.get("digit_grouping", "WESTERN"),
                "fy_start_month": int(raw.get("fy_start_month", 1)),
                "fy_start_day": int(raw.get("fy_start_day", 1)),
                "fy_label_template": raw.get("fy_label_template", "FY {start_year}"),
                "sub_jurisdictions": raw.get("sub_jurisdictions", []),
                "entity_types": raw.get("entity_types", []),
                "registration_types": raw.get("registration_types", []),
                "is_published": bool(raw.get("is_published", False)),
                "contributed_by": raw.get("contributed_by", ""),
            },
        )

        for rule in raw.get("weekend_rules", []):
            WeekendRule.objects.update_or_create(
                pack=pack,
                valid_from=rule["valid_from"],
                defaults={
                    "weekend_days": rule["weekend_days"],
                    "valid_to": rule.get("valid_to"),
                    "note": rule.get("note", ""),
                },
            )

        for authority in raw.get("authorities", []):
            Authority.objects.update_or_create(
                code=authority["code"],
                defaults={
                    "name": authority["name"],
                    "short_name": authority.get("short_name", ""),
                    "pack": pack,
                    "sub_jurisdiction": authority.get("sub_jurisdiction", ""),
                    "website": authority.get("website", ""),
                    "portal_url": authority.get("portal_url", ""),
                },
            )

        holidays = 0
        for entry in raw.get("holiday_calendars", []):
            calendar, _ = HolidayCalendar.objects.update_or_create(
                key=entry["key"],
                defaults={
                    "name": entry["name"],
                    "pack": pack,
                    "sub_jurisdiction": entry.get("sub_jurisdiction", ""),
                },
            )
            for holiday in entry.get("holidays", []):
                Holiday.objects.update_or_create(
                    calendar=calendar,
                    date=holiday["date"],
                    defaults={
                        "name": holiday["name"],
                        "is_restricted": bool(holiday.get("is_restricted", False)),
                    },
                )
                holidays += 1

        verb = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {pack.country} pack from {source.name}: "
                f"{len(raw.get('authorities', []))} authorities, {holidays} holidays."
            )
        )
