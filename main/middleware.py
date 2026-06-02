import logging
import os

from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware, RejectRequest, get_token


logger = logging.getLogger(__name__)

IS_RENDER = os.environ.get("RENDER") == "true" or bool(
    os.environ.get("RENDER_EXTERNAL_HOSTNAME")
)


class EnsureAdminMiddleware:
    """Один раз на Render создаёт admin (без запроса к БД при импорте wsgi)."""

    _bootstrapped = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if IS_RENDER and not EnsureAdminMiddleware._bootstrapped:
            try:
                from main.bootstrap import ensure_default_admin

                ensure_default_admin()
                EnsureAdminMiddleware._bootstrapped = True
            except Exception as exc:
                logger.warning("ensure_default_admin failed: %s", exc)
        return self.get_response(request)


class RenderCsrfOriginMiddleware:
    """
    На Render добавляет текущий origin (https://ваш-сервис.onrender.com)
    в CSRF_TRUSTED_ORIGINS — иначе 403 при DEBUG или смене хоста.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if IS_RENDER:
            origin = f"{request.scheme}://{request.get_host()}".rstrip("/")
            trusted = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []))
            if origin not in trusted:
                settings.CSRF_TRUSTED_ORIGINS = sorted(set(trusted + [origin]))
        return self.get_response(request)


class RenderCsrfMiddleware(CsrfViewMiddleware):
    """
    Render: гарантируем CSRF-cookie и не проверяем Origin/Referer (прокси).
    """

    def process_request(self, request):
        if IS_RENDER:
            get_token(request)
        return super().process_request(request)

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
                "CSRF token rejected on Render: %s | path=%s | host=%s | "
                "secure=%s | cookie=%s | post_token=%s",
                exc.reason,
                request.path,
                request.get_host(),
                request.is_secure(),
                settings.CSRF_COOKIE_NAME in request.COOKIES,
                bool(request.POST.get("csrfmiddlewaretoken")),
            )
            return self._reject(request, exc.reason)

        return self._accept(request)

    def _origin_verified(self, request):
        if IS_RENDER:
            return True
        return super()._origin_verified(request)

    def _check_referer(self, request):
        if IS_RENDER:
            return
        return super()._check_referer(request)


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
