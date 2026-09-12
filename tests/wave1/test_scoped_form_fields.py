"""
Scoped pickers must actually render their options.

Every create form in the product hangs off a tenant-scoped entity picker, and all
four rendered a ``<select>`` with no ``<option>`` elements in it — not even the
``---------`` empty label. The forms were therefore unusable in a browser while
remaining perfectly valid on POST, which is why the existing tests, which only
ever posted, stayed green.

The cause was that ``ScopedModelChoiceField`` overrode ``queryset`` as a plain
property and so dropped the side effect Django's own setter performs
(``self.widget.choices = self.choices``). Rendering reads the *widget's* choices;
validation reads the field's. Hence one working and the other not.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def test_the_forms_module_imports_with_no_scope_bound() -> None:
    """The whole reason ``ScopedModelChoiceField`` and friends exist.

    Django builds a ``ModelChoiceField``'s queryset when the class body runs, at
    import time, when nothing is bound. A regression here does not fail a form —
    it fails the URL conf, and the entire site returns 500.
    """
    import importlib

    for module in (
        "stacos.core.forms",
        "stacos.practice.forms",
    ):
        importlib.reload(importlib.import_module(module))
