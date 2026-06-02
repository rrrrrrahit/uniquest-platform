"""
База знаний UniQuest — полностью автономный модуль (v3).

Поиск: текст из PDF/DOCX/поля content_text, без sentence-transformers и без views.ai_assistant.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q, QuerySet
from django.shortcuts import render

from main.models import Course, Enrollment, Lecture, Profile, ScheduleEntry, Student

logger = logging.getLogger(__name__)

KB_BUILD_ID = "2026-06-03-v5"
_TOKEN_RE = re.compile(r"[\wа-яёА-ЯЁ]+", re.UNICODE)
_RU_STOP = frozenset(
    "и в во на с со по для что как это а но или не о об от до из у к же ли бы все при так их".split()
)


# ---------------------------------------------------------------------------
# Текст лекции (файл + БД)
# ---------------------------------------------------------------------------


def _lecture_file_name(lecture: Lecture) -> str:
    file_field = getattr(lecture, "lecture_file", None)
    if not file_field or not file_field.name:
        return ""
    return Path(file_field.name).name


def _extract_file_text(lecture: Lecture) -> str:
    file_field = getattr(lecture, "lecture_file", None)
    if not file_field or not file_field.name:
        return ""
    name = file_field.name.lower()
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1]
    try:
        if not file_field.storage.exists(file_field.name):
            logger.warning("Lecture %s: file not on disk %s", lecture.pk, file_field.name)
            return ""
        with file_field.open("rb") as handle:
            raw = handle.read()
    except Exception as exc:
        logger.warning("Lecture %s: read failed: %s", lecture.pk, exc)
        return ""
    if not raw:
        return ""

    if ext in {"txt", "md", "csv", "json", "log"}:
        for enc in ("utf-8", "utf-8-sig", "cp1251", "latin1"):
            try:
                return raw.decode(enc).strip()
            except Exception:
                continue
        return ""

    if ext == "pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception:
            return ""

    if ext == "docx":
        try:
            from docx import Document

            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text).strip()
        except Exception:
            return ""
    return ""


def lecture_body(lecture: Lecture) -> str:
    """Полный текст для поиска: content_text или извлечение из файла."""
    stored = (lecture.content_text or "").strip()
    if stored:
        return stored
    extracted = _extract_file_text(lecture).strip()
    if extracted:
        lecture.content_text = extracted
        try:
            lecture.save(update_fields=["content_text"])
        except Exception:
            pass
        return extracted
    return (lecture.title or "").strip()


def _snippet(text: str, query: str, max_len: int = 360) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if not query:
        return text[:max_len] + ("…" if len(text) > max_len else "")
    low = text.lower()
    qlow = query.lower()
    pos = low.find(qlow)
    if pos < 0:
        for term in _terms(query):
            pos = low.find(term)
            if pos >= 0:
                break
    if pos < 0:
        return text[:max_len] + ("…" if len(text) > max_len else "")
    start = max(0, pos - max_len // 3)
    end = min(len(text), start + max_len)
    part = text[start:end].strip()
    if start > 0:
        part = "…" + part
    if end < len(text):
        part += "…"
    return part


def _terms(query: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((query or "").lower()) if len(t) >= 2 and t not in _RU_STOP]


# ---------------------------------------------------------------------------
# Доступ к лекциям
# ---------------------------------------------------------------------------


def _enroll_from_materials(student: Student) -> None:
    if not student:
        return
    ids = set(
        Lecture.objects.exclude(content_text="").values_list("course_id", flat=True)
    )
    ids.update(
        Lecture.objects.exclude(lecture_file="").values_list("course_id", flat=True)
    )
    for cid in ids:
        if cid:
            Enrollment.objects.get_or_create(student=student, course_id=cid)


def _user_profile(user: User) -> Optional[Profile]:
    return Profile.objects.filter(user_id=user.pk).first()


def _student_courses(user: User) -> Tuple[Any, List[Course]]:
    profile = _user_profile(user)
    try:
        student = Student.objects.get(user=user)
    except Student.DoesNotExist:
        student = None

    course_ids = set(
        Enrollment.objects.filter(student__user=user).values_list("course_id", flat=True)
    )
    if profile and profile.role == Profile.ROLE_STUDENT:
        q = Q(groups__user=user)
        if profile.group_id:
            q |= Q(groups__group_id=profile.group_id)
        course_ids.update(ScheduleEntry.objects.filter(q).values_list("course_id", flat=True))

    if student and not Enrollment.objects.filter(student=student).exists():
        _enroll_from_materials(student)

    if student:
        for cid in course_ids:
            if cid:
                Enrollment.objects.get_or_create(student=student, course_id=cid)

    return student, list(Course.objects.filter(id__in=course_ids).order_by("name"))


def visible_lectures(user: User) -> QuerySet:
    profile = _user_profile(user)
    if user.is_staff and not profile:
        return Lecture.objects.select_related("course").all()
    if profile and profile.role == Profile.ROLE_TEACHER:
        return Lecture.objects.select_related("course").filter(course__teacher_id=user.pk)
    if Course.objects.filter(teacher_id=user.pk).exists():
        return Lecture.objects.select_related("course").filter(course__teacher_id=user.pk)
    _, courses = _student_courses(user)
    if courses:
        return Lecture.objects.select_related("course").filter(course__in=courses)
    return Lecture.objects.none()


def recent_materials(user: User, limit: int = 12) -> List[Lecture]:
    qs = visible_lectures(user).order_by("-created_at")
    return list(
        qs.filter(
            Q(lecture_file__isnull=False)
            | ~Q(content_url="")
            | ~Q(content_text="")
        ).exclude(lecture_file="")[:limit]
    )


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------


def _db_match_ids(user: User, query: str, terms: List[str]) -> set[int]:
    """Быстрый отбор по полям БД (работает даже без извлечённого текста файла)."""
    base = visible_lectures(user)
    q_lower = query.lower()
    db_q = Q(title__icontains=query) | Q(content_text__icontains=query) | Q(course__name__icontains=query)
    if q_lower:
        db_q |= Q(lecture_file__icontains=q_lower.replace(" ", "_"))
        db_q |= Q(lecture_file__icontains=q_lower)
    for term in terms:
        db_q |= Q(title__icontains=term) | Q(content_text__icontains=term) | Q(course__name__icontains=term)
        db_q |= Q(lecture_file__icontains=term)
    return set(base.filter(db_q).values_list("pk", flat=True))


def _score_lecture(lec: Lecture, query: str, terms: List[str], q_lower: str) -> float:
    body = lecture_body(lec)
    title = (lec.title or "").strip()
    course_name = (getattr(lec.course, "name", None) or "").strip()
    fname = _lecture_file_name(lec).lower()
    hay = f"{title}\n{course_name}\n{fname}\n{body}".lower()
    if not hay.strip():
        return 0.0

    score = 0.0
    if q_lower in title.lower():
        score += 60.0
    if fname and q_lower in fname:
        score += 45.0
    if q_lower in hay:
        score += 35.0
    for term in terms:
        if term in title.lower():
            score += 14.0
        if fname and term in fname:
            score += 12.0
        if term in hay:
            score += min(hay.count(term) * 4.0, 24.0)
        elif len(term) >= 4:
            stem = term[: max(4, len(term) - 2)]
            if stem and stem in hay:
                score += 10.0
    return score


def search_lectures(user: User, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if len(query) < 2:
        return []

    terms = _terms(query) or [query.lower()]
    q_lower = query.lower()
    base = visible_lectures(user)
    db_ids = _db_match_ids(user, query, terms)

    seen: set[int] = set()
    candidates: List[Lecture] = []
    if db_ids:
        for lec in base.filter(pk__in=db_ids).select_related("course"):
            candidates.append(lec)
            seen.add(lec.pk)
    for lec in base.select_related("course").order_by("-created_at")[:250]:
        if lec.pk not in seen:
            candidates.append(lec)
            seen.add(lec.pk)

    scored: List[Tuple[float, Lecture]] = []
    for lec in candidates:
        score = _score_lecture(lec, query, terms, q_lower)
        if score > 0 or lec.pk in db_ids:
            scored.append((max(score, 25.0 if lec.pk in db_ids else score), lec))

    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for score, lec in scored[:limit]:
        body = lecture_body(lec)
        out.append(
            {
                "id": lec.id,
                "title": lec.title,
                "score": min(score, 100.0),
                "snippet": _snippet(body, query) or title,
                "url": lec.content_url,
                "lecture": lec,
                "course_id": lec.course_id,
            }
        )
    return out


# ---------------------------------------------------------------------------
# View (единственная точка входа для /ai-assistant/)
# ---------------------------------------------------------------------------


@login_required
def knowledge_base_view(request):
    user = request.user
    profile = _user_profile(user)
    specialty = (
        profile.specialty
        if profile and getattr(profile, "specialty", None)
        else None
    )
    is_teacher = bool(profile and profile.role == Profile.ROLE_TEACHER)
    user_courses: List[Course] = []

    if is_teacher:
        user_courses = list(Course.objects.filter(teacher=user))
    elif profile and profile.role == Profile.ROLE_STUDENT:
        student_obj, user_courses = _student_courses(user)
        if student_obj and not user_courses:
            messages.info(
                request,
                "Вы пока не записаны на дисциплины с материалами. Обратитесь к преподавателю.",
            )

    query = (request.GET.get("q") or "").strip()
    search_results: List[Dict[str, Any]] = []
    recent = recent_materials(user)

    if query:
        if len(query) < 2:
            messages.info(request, "Введите минимум 2 символа для поиска.")
        else:
            try:
                search_results = search_lectures(user, query, limit=10)
            except Exception:
                logger.exception("KB search failed for q=%r", query)
                messages.error(
                    request,
                    "Ошибка поиска. Попробуйте другое слово из лекции.",
                )
            if not search_results and visible_lectures(user).exists():
                messages.info(
                    request,
                    "По этому слову совпадений нет. Попробуйте слово из названия файла/лекции "
                    "или другое слово из PDF. Ниже — все ваши доступные материалы.",
                )

    return render(
        request,
        "main/ai_assistant.html",
        {
            "query": query,
            "search_results": search_results,
            "suggested_questions": [],
            "popular_questions": [],
            "specialty": specialty,
            "student_courses": user_courses,
            "focus_areas": [],
            "is_teacher": is_teacher,
            "recent_materials": recent,
            "search_backend": f"текст файлов (сборка {KB_BUILD_ID})",
            "kb_build_id": KB_BUILD_ID,
        },
    )
