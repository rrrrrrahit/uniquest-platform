import logging
import os

from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware, RejectRequest


logger = logging.getLogger(__name__)

IS_RENDER = os.environ.get("RENDER") == "true" or bool(
    os.environ.get("RENDER_EXTERNAL_HOSTNAME")
)


class RenderCsrfMiddleware(CsrfViewMiddleware):
    """
    На Render: проверяем только CSRF-токен (cookie = POST).
    Origin/Referer за reverse proxy часто дают ложный 403.
    """

    def process_view(self, request, callback, callback_args, callback_kwargs):
        if not IS_RENDER:
            return super().process_view(request, callback, callback_args, callback_kwargs)

        if getattr(request, "csrf_processing_done", False):
            return None
        if getattr(callback, "csrf_exempt", False):
            return None
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return self._accept(request)
        if getattr(request, "_dont_enforce_csrf_checks", False):
            return self._accept(request)

        try:
            self._check_token(request)
        except RejectRequest as exc:
            logger.warning(
                "CSRF token rejected on Render: %s path=%s host=%s cookie=%s",
                exc.reason,
                request.path,
                request.get_host(),
                settings.CSRF_COOKIE_NAME in request.COOKIES,
            )
            return self._reject(request, exc.reason)

        return self._accept(request)


class AccessAuditMiddleware:
    """Lightweight audit for forbidden access on protected routes."""

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
