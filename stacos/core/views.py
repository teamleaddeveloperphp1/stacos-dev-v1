"""Core views. Deliberately almost empty — this app is infrastructure."""

from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, JsonResponse

from stacos.core.permissions import public_view


@public_view
def healthz(request: HttpRequest) -> JsonResponse:  # noqa: ARG001 - Django view signature
    """Liveness and readiness probe.

    Checks the database, because a process that cannot reach PostgreSQL is not
    ready to serve even though it will happily answer HTTP.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        database_ok = True
    except Exception:
        database_ok = False

    status = 200 if database_ok else 503
    return JsonResponse(
        {"status": "ok" if database_ok else "degraded", "db": database_ok}, status=status
    )
