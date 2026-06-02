"""
База знаний: видимость лекций и поиск.
Отдельный модуль — без локальных import Lecture внутри views (UnboundLocalError).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Q, QuerySet

from .lecture_utils import lecture_is_searchable
from .models import Course, Enrollment, Lecture, Profile, ScheduleEntry, Student
from .search_service import (
    build_lecture_snippet,
    hybrid_search_for_lectures,
    search_backend_label,
)

logger = logging.getLogger(__name__)


def _supports_lecture_file() -> bool:
    return "lecture_file" in {f.name for f in Lecture._meta.get_fields()}


def recent_materials_queryset(base_qs: QuerySet) -> QuerySet:
    qs = base_qs.select_related("course").order_by("-created_at")
    if _supports_lecture_file():
        return qs.filter(
            Q(lecture_file__isnull=False)
            | ~Q(content_url="")
            | ~Q(content_text="")
        ).exclude(lecture_file="")
    return qs.filter(~Q(content_url="") | ~Q(content_text=""))


def _enroll_student_in_available_courses(student: Student) -> int:
    if not student:
        return 0

    course_ids = set()
    if student.group_id:
        course_ids.update(
            Enrollment.objects.filter(student__group_id=student.group_id).values_list(
                "course_id", flat=True
            )
        )
        course_ids.update(
            ScheduleEntry.objects.filter(groups__group_id=student.group_id).values_list(
                "course_id", flat=True
            )
        )

    course_ids.update(
        Lecture.objects.exclude(content_text="")
        .exclude(content_text__isnull=True)
        .values_list("course_id", flat=True)
        .distinct()
    )
    course_ids.update(
        Lecture.objects.exclude(lecture_file="")
        .exclude(lecture_file__isnull=True)
        .values_list("course_id", flat=True)
        .distinct()
    )
    course_ids.update(
        Lecture.objects.exclude(content_url="")
        .exclude(content_url__isnull=True)
        .values_list("course_id", flat=True)
        .distinct()
    )

    created = 0
    for course_id in course_ids:
        if not course_id:
            continue
        _, was_created = Enrollment.objects.get_or_create(
            student=student,
            course_id=course_id,
        )
        if was_created:
            created += 1
    return created


def student_enrollments(user: User):
    profile = getattr(user, "profile", None)
    try:
        student = Student.objects.get(user=user)
    except Student.DoesNotExist:
        student = None

    course_ids = set(
        Enrollment.objects.filter(student__user=user).values_list("course_id", flat=True)
    )
    if profile and profile.role == Profile.ROLE_STUDENT:
        schedule_q = Q(groups__user=user)
        if profile.group_id:
            schedule_q |= Q(groups__group_id=profile.group_id)
        course_ids.update(
            ScheduleEntry.objects.filter(schedule_q).values_list("course_id", flat=True)
        )

    if student and not Enrollment.objects.filter(student=student).exists():
        _enroll_student_in_available_courses(student)

    if student:
        for course_id in course_ids:
            Enrollment.objects.get_or_create(student=student, course_id=course_id)

    courses = list(Course.objects.filter(id__in=course_ids).order_by("name"))
    return student, courses


def lectures_visible_to_user(user: User) -> QuerySet:
    profile = getattr(user, "profile", None)
    if user.is_staff and not profile:
        return Lecture.objects.select_related("course").all()
    if profile and profile.role == Profile.ROLE_TEACHER:
        return Lecture.objects.select_related("course").filter(course__teacher=user)
    _, courses = student_enrollments(user)
    if courses:
        return Lecture.objects.select_related("course").filter(course__in=courses)
    return Lecture.objects.none()


def search_lectures_for_user(user: User, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    fast = getattr(settings, "SEARCH_FAST_MODE", True)
    return hybrid_search_for_lectures(
        query,
        lectures_visible_to_user(user),
        limit=limit,
        fast=fast,
    )


def build_page_context(request) -> Dict[str, Any]:
    user = request.user
    profile = getattr(user, "profile", None)
    specialty = (
        profile.specialty
        if profile and hasattr(profile, "specialty") and profile.specialty
        else None
    )
    is_teacher = bool(profile and profile.role == Profile.ROLE_TEACHER)

    user_courses: List[Course] = []
    if is_teacher:
        user_courses = list(Course.objects.filter(teacher=user))
    elif profile and profile.role == Profile.ROLE_STUDENT:
        student_obj, user_courses = student_enrollments(user)
        if student_obj and not user_courses:
            messages.info(
                request,
                "Вы пока не записаны на дисциплины с материалами. Обратитесь к преподавателю.",
            )

    query = (request.GET.get("q") or "").strip()
    search_results: List[Dict[str, Any]] = []
    visible_lectures = lectures_visible_to_user(user)
    recent_materials = list(recent_materials_queryset(visible_lectures)[:12])

    if query:
        if len(query) < 2:
            messages.info(request, "Введите минимум 2 символа для поиска.")
        else:
            for result in search_lectures_for_user(user, query, limit=10):
                lecture = result.get("lecture")
                if lecture and not lecture_is_searchable(lecture):
                    continue
                if lecture and not (result.get("snippet") or "").strip():
                    result["snippet"] = build_lecture_snippet(lecture, query, max_len=360)
                search_results.append(result)

            if not search_results and visible_lectures.exists():
                messages.info(
                    request,
                    "По запросу ничего не найдено. Попробуйте слова из текста лекции или названия.",
                )

    return {
        "query": query,
        "search_results": search_results,
        "suggested_questions": [],
        "popular_questions": [],
        "specialty": specialty,
        "student_courses": user_courses,
        "focus_areas": [],
        "is_teacher": is_teacher,
        "recent_materials": recent_materials,
        "search_backend": search_backend_label(fast=getattr(settings, "SEARCH_FAST_MODE", True)),
    }


def build_error_context(request) -> Dict[str, Any]:
    return {
        "query": (request.GET.get("q") or "").strip(),
        "search_results": [],
        "suggested_questions": [],
        "popular_questions": [],
        "specialty": None,
        "student_courses": [],
        "focus_areas": [],
        "is_teacher": False,
        "recent_materials": [],
        "search_backend": search_backend_label(fast=True),
    }
