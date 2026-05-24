"""Помощники для лекций: доступ, скачивание, поиск."""

from __future__ import annotations

import io
from pathlib import Path

from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect

from .models import Lecture


def lecture_has_downloadable_content(lecture: Lecture) -> bool:
    if (lecture.content_text or "").strip():
        return True
    if (lecture.content_url or "").strip():
        return True
    if not lecture.lecture_file:
        return False
    try:
        return lecture.lecture_file.storage.exists(lecture.lecture_file.name)
    except Exception:
        return bool(lecture.lecture_file.name)


def lecture_is_searchable(lecture: Lecture) -> bool:
    """Лекция участвует в поиске (есть текст, ссылка, файл или хотя бы название)."""
    if (lecture.content_text or "").strip():
        return True
    if (lecture.content_url or "").strip():
        return True
    if lecture.lecture_file:
        return True
    return bool((lecture.title or "").strip())


def build_lecture_download_response(lecture: Lecture):
    """
    Отдаёт файл, текст лекции (.txt) или редирект на ссылку.
    None — если скачать нечего.
    """
    if lecture.lecture_file:
        try:
            if lecture.lecture_file.storage.exists(lecture.lecture_file.name):
                handle = lecture.lecture_file.open("rb")
                filename = Path(lecture.lecture_file.name).name
                response = FileResponse(handle, as_attachment=True, filename=filename)
                response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                return response
        except Exception:
            pass

    text = (lecture.content_text or "").strip()
    if text:
        safe_name = "".join(c for c in (lecture.title or "lecture") if c.isalnum() or c in " _-")[:80]
        filename = f"{safe_name or 'lecture'}.txt"
        response = HttpResponse(text, content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    url = (lecture.content_url or "").strip()
    if url:
        return redirect(url)

    return None
