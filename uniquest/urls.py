from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from main.kb import KB_BUILD_ID, knowledge_base_view


def deploy_version(request):
    """Публичная проверка: какой код реально на сервере (без входа)."""
    return HttpResponse(
        f"uniquest_kb_build={KB_BUILD_ID}\n",
        content_type="text/plain; charset=utf-8",
    )


# База знаний — в корневых URL (раньше include), чтобы не подхватывался старый views.ai_assistant
urlpatterns = [
    path("__deploy_version__/", deploy_version),
    path("ai-assistant/", knowledge_base_view, name="ai_assistant"),
    path("admin/", admin.site.urls),
    path("", include("main.urls")),
]

# Для раздачи медиа файлов в development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
