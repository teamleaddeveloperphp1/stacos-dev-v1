"""
Minimal models for testing the scoping machinery in isolation.

Testing against real models is necessary but not sufficient: a real model comes
with constraints, validators and business rules that can mask *why* something
was rejected. These two exist so a test can assert that the manager and the write
guard behave correctly with nothing else in the way.
"""

from typing import ClassVar

from django.db import models

from stacos.core.models import TenantScopedModel


class ScopedThing(TenantScopedModel):
    """Tenant-owned, not about any particular entity."""

    label = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"

    def __str__(self) -> str:
        return self.label


class EntityScopedThing(TenantScopedModel):
    """Tenant-owned *and* about a specific entity, so engagement scoping applies."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.ForeignKey("tenancy.Entity", on_delete=models.CASCADE)
    label = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"

    def __str__(self) -> str:
        return self.label
