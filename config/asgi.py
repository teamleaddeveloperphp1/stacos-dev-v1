"""
ASGI entrypoint.

Served by Uvicorn alongside the Gunicorn/WSGI process. STACOS needs ASGI only for
the one-way Server-Sent Events notification stream — one connection per session
carrying badge counts and toasts. That is all this product's real-time needs
amount to, and it is dramatically simpler than websockets.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_asgi_application()
