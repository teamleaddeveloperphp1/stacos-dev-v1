"""Upload form for the vault."""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from stacos.core.forms import ScopedModelChoiceField
from stacos.tenancy.models import Entity
from stacos.vault.models import Document, DocumentKind

__all__ = ["UploadForm"]

#: Extensions a compliance product legitimately receives. An allowlist rather
#: than a blocklist: the set of dangerous extensions is open-ended and grows,
#: while the set of things a client sends an accountant does not.
ALLOWED_EXTENSIONS = frozenset(
    {
        "pdf",
        "png",
        "jpg",
        "jpeg",
        "tiff",
        "webp",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "csv",
        "txt",
        "rtf",
        "zip",
        "xml",
        "json",
    }
)

MAX_UPLOAD_BYTES = getattr(settings, "STACOS_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A file field that accepts several files.

    Django's ``FileField`` deliberately handles one; a vault upload is almost
    always a batch, and asking a client to repeat the dialogue six times is how
    they stop using the product.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data: Any, initial: Any = None) -> list[Any]:
        single = super().clean
        if isinstance(data, list | tuple):
            return [single(item, initial) for item in data]
        return [single(data, initial)]


class UploadForm(forms.Form):
    entity = ScopedModelChoiceField(
        Entity, filters={"archived_at__isnull": True}, label=_("Entity")
    )
    # A batch, not a single file. The base class types this as one upload;
    # overriding it is the point of the field, so the narrowing is deliberate.
    files = MultipleFileField(label=_("Files"))  # type: ignore[assignment]
    title = forms.CharField(
        required=False,
        max_length=250,
        label=_("Title"),
        help_text=_("Leave blank to use the filename."),
    )
    kind = forms.ChoiceField(choices=DocumentKind.choices, initial=DocumentKind.EVIDENCE)
    classification = forms.ChoiceField(
        choices=Document.Classification.choices,
        initial=Document.Classification.GENERAL,
        help_text=_("Drives retention and who may download it."),
    )
    period_key = forms.CharField(required=False, max_length=32, label=_("Period"))
    evidence_key = forms.CharField(required=False, max_length=60, widget=forms.HiddenInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False

    def clean_files(self) -> list[Any]:
        files = self.cleaned_data["files"]
        for upload in files:
            name = (upload.name or "").lower()
            extension = name.rsplit(".", 1)[-1] if "." in name else ""
            if extension not in ALLOWED_EXTENSIONS:
                raise forms.ValidationError(
                    _("%(name)s is not a file type we accept.") % {"name": upload.name}
                )
            if (upload.size or 0) > MAX_UPLOAD_BYTES:
                raise forms.ValidationError(
                    _("%(name)s is larger than the %(limit)d MB limit.")
                    % {"name": upload.name, "limit": MAX_UPLOAD_BYTES // (1024 * 1024)}
                )
        return files
