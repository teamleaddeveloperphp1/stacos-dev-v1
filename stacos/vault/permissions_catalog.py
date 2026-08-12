"""Permissions over the document vault."""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Documents"

VIEW = "vault.document.view"

permission_registry.register_many(
    [
        Permission(code=VIEW, label="View documents", category=CATEGORY),
        Permission(
            code="vault.document.upload",
            label="Upload documents",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="vault.document.download",
            label="Download documents",
            category=CATEGORY,
            description=(
                "Separate from viewing the list. A user may legitimately see that "
                "a document exists without being able to take a copy of it, and "
                "every download is recorded against the file."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="vault.document.delete",
            label="Archive a document",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Documents are evidence. Archiving hides one from the working "
                "view and never destroys it, but it still needs re-authentication."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="vault.document.export",
            label="Bulk export documents",
            category=CATEGORY,
            is_sensitive=True,
            description="Takes a copy of everything at once. The highest-value action in the product to an attacker.",
            implies=frozenset({VIEW, "vault.document.download"}),
        ),
    ]
)
