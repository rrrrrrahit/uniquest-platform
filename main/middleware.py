import logging
import os
from urllib.parse import urlparse

from django.conf import settings


logger = logging.getLogger(__name__)


class RenderCsrfOriginMiddleware:
    """
    На Render доверяем Origin/Referer текущего запроса (точный хост без wildcards).
    Должен стоять ПЕРЕД CsrfViewMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if os.environ.get("RENDER") == "true" or os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
            origin = (request.META.get("HTTP_ORIGIN") or "").strip().rstrip("/")
            if not origin:
                referer = (request.META.get("HTTP_REFERER") or "").strip()
                if referer:
                    parsed = urlparse(referer)
                    if parsed.scheme and parsed.netloc:
                        origin = f"{parsed.scheme}://{parsed.netloc}"

            if origin and origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS = list(settings.CSRF_TRUSTED_ORIGINS) + [origin]

        return self.get_response(request)


class AccessAuditMiddleware:
    """
    Lightweight audit for forbidden access attempts on protected routes.
    """

    PROTECTED_PREFIXES = (
        "/teacher/",
        "/student-passport/",
        "/ai-learning-assistant/",
        "/api/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            response.status_code == 403
            and request.path.startswith(self.PROTECTED_PREFIXES)
        ):
            username = (
                request.user.username
                if getattr(request, "user", None) and request.user.is_authenticated
                else "anonymous"
            )
            logger.warning(
                "403 access denied: user=%s path=%s method=%s",
                username,
                request.path,
                request.method,
            )
        return response
