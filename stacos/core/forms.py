"""
Form fields that cooperate with tenant scoping.

A ``ModelForm`` with a foreign key to a tenant-scoped model does not work in this
codebase, and the failure is spectacular rather than subtle: Django builds the
field's default queryset when the *class* is defined, which happens at import
time, when no :class:`~stacos.core.scope.AccessScope` is bound — so the module
raises ``UnscopedQueryError`` and the whole URL conf fails to load.

The obvious workarounds are both wrong. Passing ``Model.objects.none()`` hits the
same guard. Assigning the real queryset in ``__init__`` works, but every form has
to remember, and the one that forgets renders a picker listing every tenant's
entities.

:class:`ScopedModelChoiceField` resolves its queryset at the moment it is *used*
— rendered or validated — which is inside a request, with a scope bound. That
also makes it a genuine authorisation boundary rather than a rendering
convenience: a forged POST naming another tenant's entity is re-queried under the
current scope at ``clean()`` time and simply does not resolve.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.db.models import Manager, Model, QuerySet

__all__ = ["ScopedModelChoiceField", "ScopedModelMultipleChoiceField"]


class _LazyScopedQuerysetMixin:
    """Resolve ``queryset`` on access rather than at construction."""

    scoped_model: type[Model]
    scoped_filters: dict[str, Any]
    _queryset: QuerySet[Any] | None

    def _get_queryset(self) -> QuerySet[Any]:
        if self._queryset is not None:
            return self._queryset
        # The scoped manager, so this raises loudly outside a request — which is
        # correct. There is no legitimate reason to render or validate one of
        # these fields without a scope. Reached through `_default_manager` rather
        # than `objects` so the annotation is honest: `type[Model]` has no
        # `objects` until a subclass declares one.
        manager: Manager[Any] = self.scoped_model._default_manager
        return manager.filter(**self.scoped_filters)

    def _set_queryset(self, value: QuerySet[Any] | None) -> None:
        self._queryset = value

    queryset = property(_get_queryset, _set_queryset)


class ScopedModelChoiceField(_LazyScopedQuerysetMixin, forms.ModelChoiceField[Any]):
    """A ``ModelChoiceField`` over a tenant-scoped model.

    >>> entity = ScopedModelChoiceField(Entity, filters={"archived_at__isnull": True})
    """

    def __init__(
        self,
        model: type[Model],
        *,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.scoped_model = model
        self.scoped_filters = filters or {}
        super().__init__(queryset=None, **kwargs)


class ScopedModelMultipleChoiceField(_LazyScopedQuerysetMixin, forms.ModelMultipleChoiceField[Any]):
    """The many-valued form of :class:`ScopedModelChoiceField`."""

    def __init__(
        self,
        model: type[Model],
        *,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.scoped_model = model
        self.scoped_filters = filters or {}
        super().__init__(queryset=None, **kwargs)
