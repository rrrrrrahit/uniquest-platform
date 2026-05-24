"""
WSGI config for uniquest project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'uniquest.settings')

application = get_wsgi_application()

# Гарантируем admin при старте gunicorn (Render Shell часто read-only).
import logging

_wsgi_log = logging.getLogger("uniquest.wsgi")
try:
    from main.bootstrap import ensure_default_admin

    ensure_default_admin()
    _wsgi_log.info("Default admin user ensured on startup.")
except Exception as exc:
    _wsgi_log.warning("ensure_default_admin on startup failed: %s", exc)
