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

**Why the widget is wired up by hand.** Django's ``ModelChoiceField.queryset``
setter carries a side effect that is easy to miss and fatal to lose::

    def _set_queryset(self, queryset):
        self._queryset = None if queryset is None else queryset.all()
        self.widget.choices = self.choices     # <- this line

Rendering reads the *widget's* choices, not the field's. Overriding ``queryset``
as a plain property drops that assignment, and every scoped ``<select>`` renders
with no options at all — not even the ``---------`` empty label — while
validation carries on working perfectly, because ``to_python`` goes through the
field. That combination is why it survived so long: the forms accepted a valid
POST, so the tests passed, and nobody could choose anything in the browser.

Restoring the assignment naively reintroduces the original disease, because
``ModelChoiceIterator`` captures ``field.queryset`` in its constructor and that
constructor runs at import time. :class:`_LazyModelChoiceIterator` defers the
read to iteration, which is what lets the widget be wired up eagerly and still
resolved lazily.
"""

from __future__ import annotations

from typing import Any, cast

from django import forms
from django.db.models import Manager, Model, QuerySet
from django.forms.models import ModelChoiceIterator

__all__ = [
    "ScopedModelChoiceField",
    "ScopedModelMultipleChoiceField",
    "ScopedUserChoiceField",
]


class _LazyModelChoiceIterator(ModelChoiceIterator[Any]):
    """A choice iterator that reads its queryset when iterated, not when built.

    Django's version does ``self.queryset = field.queryset`` in ``__init__``.
    That is fine for an ordinary field and impossible for a scoped one: the
    iterator is constructed while the form class is being defined, long before a
    request has bound a scope, so reading the queryset there raises
    ``UnscopedQueryError`` at import time.

    Deferring costs nothing — every consumer (``__iter__``, ``__len__``,
    ``__bool__``) already reaches the queryset through ``self.queryset``.
    """

    def __init__(self, field: forms.ModelChoiceField[Any]) -> None:
        self.field = field

    @property
    def queryset(self) -> QuerySet[Any]:
        return cast("QuerySet[Any]", self.field.queryset)

    @queryset.setter
    def queryset(self, _value: QuerySet[Any]) -> None:
        # Django never assigns this, but the base class declares it as an
        # ordinary instance attribute. Accepting and ignoring a write keeps the
        # substitution honest rather than raising on an inherited code path.
        return None


class _LazyScopedQuerysetMixin:
    """Resolve ``queryset`` on access rather than at construction."""

    scoped_model: type[Model]
    scoped_filters: dict[str, Any]
    _queryset: QuerySet[Any] | None

    def resolve_queryset(self) -> QuerySet[Any]:
        """Build the queryset under the currently bound scope.

        Overridden by :class:`ScopedUserChoiceField`, which narrows a model that
        is not itself tenant-scoped. A method rather than a second property, so
        subclasses can replace it without redeclaring the property — Python
        binds a property's getter once, at class-definition time, so overriding
        ``_get_queryset`` alone would have no effect.
        """
        # The scoped manager, so this raises loudly outside a request — which is
        # correct. There is no legitimate reason to render or validate one of
        # these fields without a scope. Reached through `_default_manager` rather
        # than `objects` so the annotation is honest: `type[Model]` has no
        # `objects` until a subclass declares one.
        manager: Manager[Any] = self.scoped_model._default_manager
        return manager.filter(**self.scoped_filters)

    def _get_queryset(self) -> QuerySet[Any]:
        if self._queryset is not None:
            return self._queryset
        return self.resolve_queryset()

    def _set_queryset(self, value: QuerySet[Any] | None) -> None:
        self._queryset = value
        # The assignment Django performs and this class used to drop, which is
        # what left every scoped `<select>` rendering zero options. `self.choices`
        # builds a `_LazyModelChoiceIterator`, so no query is issued here — the
        # widget simply receives something that knows how to find the options
        # when it is time to render them.
        field = cast(Any, self)
        field.widget.choices = field.choices

    queryset = property(_get_queryset, _set_queryset)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        """Copy the field without resolving its queryset.

        ``BaseForm.__init__`` deep-copies ``base_fields`` on every instantiation,
        and ``ModelChoiceField.__deepcopy__`` pins ``result.queryset =
        self.queryset.all()`` there to force a fresh iterator. Pinning would
        resolve the scoped queryset once, at form construction, and cache it on
        the field — so the field would keep answering with the scope that built
        it. Going straight to ``Field.__deepcopy__`` and re-pointing the widget
        keeps resolution where the module docstring promises it happens: at
        render, and again at validation.
        """
        result = cast(Any, forms.Field.__deepcopy__(cast(Any, self), memo))
        # `Field.__deepcopy__` starts from a shallow copy, so the filters dict
        # would otherwise be shared between every form instance.
        result.scoped_filters = dict(self.scoped_filters)
        result.widget.choices = result.choices
        return result


class ScopedModelChoiceField(_LazyScopedQuerysetMixin, forms.ModelChoiceField[Any]):
    """A ``ModelChoiceField`` over a tenant-scoped model.

    >>> entity = ScopedModelChoiceField(Entity, filters={"archived_at__isnull": True})
    """

    #: Consulted by ``ModelChoiceField.choices``. See ``_LazyModelChoiceIterator``.
    iterator = _LazyModelChoiceIterator

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

    iterator = _LazyModelChoiceIterator

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


class ScopedUserChoiceField(_LazyScopedQuerysetMixin, forms.ModelChoiceField[Any]):
    """A picker over *people*, narrowed to the caller's own organisation.

    ``User`` is deliberately not tenant-scoped — one person is one identity
    across every tenant they belong to — so a ``ModelForm`` left to build an
    ``assigned_to`` or ``reviewer`` field itself produces a ``<select>`` listing
    every user on the platform. That is a cross-tenant disclosure (other
    customers' staff names, visible to anyone who can open the form) and a write
    hole as well: the POST validates, so a record can genuinely be assigned to
    somebody at an unrelated company who then owns work for a business they have
    no connection to.

    The narrowing goes through ``Membership``'s own scoped manager rather than
    filtering on tenant ids by hand, so it inherits the rule the rest of the
    codebase already applies to people-data: tenant-level models are filtered on
    ``member_tenant_ids``, not ``readable_tenant_ids``. A practice engaged on a
    client's entity can therefore read that client's compliance data without
    being able to enumerate their staff directory.

    Like its siblings the queryset is re-resolved at ``clean()`` time, so a
    forged user id is re-checked against the caller's scope rather than merely
    being absent from the rendered options.
    """

    iterator = _LazyModelChoiceIterator

    def __init__(self, **kwargs: Any) -> None:
        self.scoped_filters = {}
        super().__init__(queryset=None, **kwargs)

    def resolve_queryset(self) -> QuerySet[Any]:
        # Imported here rather than at module scope: this module is imported by
        # form modules that Django loads while the app registry is still being
        # populated, and `get_user_model()` needs it finished.
        from django.contrib.auth import get_user_model

        from stacos.tenancy.models import Membership

        members = Membership.objects.filter(status=Membership.Status.ACTIVE).values("user_id")
        user_model = get_user_model()
        return user_model._default_manager.filter(pk__in=members, is_active=True).order_by(
            "full_name", "email"
        )
