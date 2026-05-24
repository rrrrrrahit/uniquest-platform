from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import login, authenticate, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.csrf import ensure_csrf_cookie
from django.http import JsonResponse, FileResponse, Http404
from functools import wraps
from collections import defaultdict
from datetime import timedelta, datetime, time
from pathlib import Path
import random
import re
from .forms import (
    UserRegisterForm,
    UserUpdateForm,
    ProfileUpdateForm,
    TeacherGradeForm,
    LectureCreateForm,
    TeacherGradeEntryForm,
)
from .models import (
    Profile,
    Course,
    Assignment,
    Submission,
    Recommendation,
    ScheduleEntry,
    Grade,
    Specialty,
    Subject,
    ProblemPrediction,
    StudentProgress,
    Group,
    Student,
    Lecture,
    Enrollment,
    Attendance,
    LectureQuiz,
    LectureQuizQuestion,
    LectureQuizAttempt,
)
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.utils import timezone
from datetime import timedelta
import json

from .search_service import semantic_search, build_lecture_snippet, search_backend_label


def _supports_lecture_file() -> bool:
    return "lecture_file" in {f.name for f in Lecture._meta.get_fields()}


def _recent_materials_queryset(base_qs):
    qs = base_qs.select_related("course").order_by("-created_at")
    if _supports_lecture_file():
        return qs.filter(
            Q(lecture_file__isnull=False)
            | ~Q(content_url="")
            | ~Q(content_text="")
        ).exclude(lecture_file="")
    return qs.filter(~Q(content_url="") | ~Q(content_text=""))


# Централизованный редирект пользователя по роли.
def _role_home(user):
    # Берём роль напрямую из БД, чтобы не поймать stale-кэш profile после регистрации.
    profile_role = (
        Profile.objects.filter(user_id=user.id).values_list("role", flat=True).first()
    )
    if profile_role == Profile.ROLE_TEACHER:
        return "teacher_dashboard"
    return "dashboard"


def _user_can_access_course(user, course):
    user_profile = getattr(user, "profile", None)
    if user.is_staff and not user_profile:
        return True
    if user_profile and user_profile.role == Profile.ROLE_TEACHER:
        return course.teacher_id == user.id
    return Enrollment.objects.filter(student__user=user, course=course).exists()


def _deny_and_redirect(request, text, route_name=None):
    messages.error(request, text)
    return redirect(route_name or _role_home(request.user))


def _rate_limit_exceeded(request, scope, limit=10, window_seconds=60):
    actor = (
        f"user:{request.user.id}"
        if getattr(request, "user", None) and request.user.is_authenticated
        else f"ip:{request.META.get('REMOTE_ADDR', 'unknown')}"
    )
    cache_key = f"rl:{scope}:{actor}"
    current = cache.get(cache_key, 0)
    if current >= limit:
        return True
    cache.set(cache_key, current + 1, timeout=window_seconds)
    return False


def _sentence_candidates(source_text: str):
    text = re.sub(r"\s+", " ", (source_text or "")).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[\.\!\?])\s+", text)
    candidates = []
    for part in parts:
        s = part.strip(" -\t\r\n")
        if 25 <= len(s) <= 260:
            candidates.append(s)
    return candidates


_QUIZ_STOPWORDS = {
    "и", "или", "для", "это", "также", "как", "что", "когда", "если", "при", "над", "под",
    "the", "and", "for", "with", "from", "into", "that", "this", "which", "where",
    "они", "она", "оно", "его", "ее", "их", "без", "после", "перед", "через",
}


def _extract_keywords(sentence: str):
    raw = re.findall(r"[A-Za-zА-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ\-]{4,}", sentence or "")
    terms = []
    for token in raw:
        term = token.strip("-").lower()
        if len(term) < 4:
            continue
        if term in _QUIZ_STOPWORDS:
            continue
        if term not in terms:
            terms.append(term)
    return terms


def _definition_score(sentence: str):
    s = (sentence or "").lower()
    score = 0
    definition_markers = (
        " это ",
        " называется ",
        " определяется ",
        " представляет собой ",
        " is ",
        " are ",
    )
    if any(marker in f" {s} " for marker in definition_markers):
        score += 3
    if re.search(r"\d", s):
        score += 2
    if ":" in s:
        score += 1
    return score


def _sentence_signature(sentence: str):
    """
    Грубая сигнатура смысла для дедупликации похожих вопросов.
    """
    terms = _extract_keywords(sentence)
    if not terms:
        normalized = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9 ]+", " ", sentence or "").lower()
        return " ".join(normalized.split()[:8])
    return "|".join(sorted(terms[:8]))


def _replace_term_once(text: str, source_term: str, target_term: str):
    return re.sub(
        rf"\b{re.escape(source_term)}\b",
        target_term,
        text,
        count=1,
        flags=re.IGNORECASE,
    )


def _build_quiz_questions_from_text(source_text: str, question_count: int):
    candidates = _sentence_candidates(source_text)
    if len(candidates) < 4:
        return []

    question_count = max(3, min(question_count, 12, len(candidates)))
    # Вначале берём более информативные предложения (с терминами/цифрами).
    ranked = sorted(candidates, key=lambda s: (_definition_score(s), len(_extract_keywords(s)), len(s)), reverse=True)

    # Берем уникальные по сигнатуре предложения.
    selected = []
    seen_signatures = set()
    for sentence in ranked:
        sig = _sentence_signature(sentence)
        if sig in seen_signatures:
            continue
        selected.append(sentence)
        seen_signatures.add(sig)
        if len(selected) >= question_count:
            break
    pool = candidates.copy()
    rnd = random.Random(42)
    keyword_pool = []
    for sentence in ranked:
        for token in _extract_keywords(sentence):
            word = token.strip().lower()
            if word not in keyword_pool:
                keyword_pool.append(word)
    questions = []
    used_question_signatures = set()

    for idx, correct_text in enumerate(selected, start=1):
        distractors_pool = [s for s in pool if s != correct_text]
        if len(distractors_pool) < 3:
            continue
        distractors = rnd.sample(distractors_pool, 3)
        options = [correct_text] + distractors
        rnd.shuffle(options)
        correct_idx = options.index(correct_text)
        correct_letter = ["A", "B", "C", "D"][correct_idx]
        question_text = f"Какое утверждение соответствует материалу лекции? (Вопрос {idx})"

        # Если возможно, генерируем более "умный" вопрос с термином/фактом.
        sentence_tokens = _extract_keywords(correct_text)
        candidate_terms = [t for t in sentence_tokens if t in keyword_pool and len(t) >= 5]
        if candidate_terms:
            term = rnd.choice(candidate_terms)
            # Берём правдоподобные дистракторы: близкая длина + не из того же предложения.
            distractor_terms = [
                t for t in keyword_pool
                if t != term and t not in sentence_tokens and abs(len(t) - len(term)) <= 4
            ]
            if len(distractor_terms) < 3:
                distractor_terms = [t for t in keyword_pool if t != term and t not in sentence_tokens]

            # Тип 1: пропущенный термин.
            if len(distractor_terms) >= 3:
                term_options = [term] + rnd.sample(distractor_terms, 3)
                rnd.shuffle(term_options)
                correct_letter = ["A", "B", "C", "D"][term_options.index(term)]
                options = [opt.capitalize() for opt in term_options]
                masked_sentence = re.sub(
                    rf"\b{re.escape(term)}\b",
                    "_____",
                    correct_text,
                    flags=re.IGNORECASE,
                )
                question_text = f"Выберите пропущенный термин: «{masked_sentence}»"

            # Тип 2: одно верное утверждение, три правдоподобных искажённых.
            false_statements = []
            if len(distractor_terms) >= 3 and len(correct_text) <= 220:
                for fake_term in rnd.sample(distractor_terms, 3):
                    false_statements.append(_replace_term_once(correct_text, term, fake_term))
                options = [correct_text] + false_statements
                rnd.shuffle(options)
                correct_letter = ["A", "B", "C", "D"][options.index(correct_text)]
                question_text = "Выберите верное утверждение по материалу лекции:"

        q_signature = _sentence_signature(question_text + " " + correct_text)
        if q_signature in used_question_signatures:
            continue
        used_question_signatures.add(q_signature)

        questions.append(
            {
                "order": len(questions) + 1,
                "question_text": question_text,
                "options": options,
                "correct_letter": correct_letter,
            }
        )
    return questions


def _ensure_registration_reference_data():
    """Гарантирует, что в форме регистрации есть группы и специальность."""
    if not Group.objects.exists():
        Group.objects.bulk_create(
            [
                Group(name="ИС-Р", year=timezone.now().year),
                Group(name="ИС-К", year=timezone.now().year),
                Group(name="ВТиПО-Р", year=timezone.now().year),
            ],
            ignore_conflicts=True,
        )
    if not Specialty.objects.exists():
        Specialty.objects.create(
            code="6B061",
            name_kk="Ақпараттық-коммуникациялық технологиялар",
            name_ru="Информационно-коммуникационные технологии",
            description="Базовая специальность для первичной регистрации студентов.",
        )

# ===== Главная и авторизация =====
def index(request):
    if request.user.is_authenticated:
        return redirect(_role_home(request.user))

    return render(request, 'main/index.html')

def create_test_student_view(request):
    """Простая страница для создания тестового студента (только для staff)."""
    if not request.user.is_authenticated or not request.user.is_staff:
        messages.error(request, 'Доступ запрещён.')
        return redirect('login')

    from django.contrib.auth.models import User
    
    # Также создаем администратора, если его нет
    admin_user, admin_created = User.objects.get_or_create(
        username='admin',
        defaults={
            'email': 'admin@uniquest.kz',
            'is_staff': True,
            'is_superuser': True,
        }
    )
    admin_user.set_password('admin123456')
    admin_user.is_staff = True
    admin_user.is_superuser = True
    admin_user.is_active = True
    admin_user.save(update_fields=['password', 'is_staff', 'is_superuser', 'is_active'])
    
    if request.method == 'POST':
        try:
            # Создаем или получаем группу
            group, created = Group.objects.get_or_create(
                name='CS-101',
                defaults={'year': timezone.now().year}
            )
            
            # Создаем или получаем пользователя
            username = 'test_student'
            password = 'test123456'
            
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': 'test_student@example.com',
                    'first_name': 'Тестовый',
                    'last_name': 'Студент',
                }
            )
            
            if created:
                user.set_password(password)
                user.save()
            
            # Создаем или обновляем профиль
            profile, created = Profile.objects.get_or_create(
                user=user,
                defaults={
                    'role': Profile.ROLE_STUDENT,
                    'group': group,
                }
            )
            if not created:
                profile.group = group
                profile.role = Profile.ROLE_STUDENT
                profile.save()
            
            # Создаем или получаем студента
            student, created = Student.objects.get_or_create(
                user=user,
                defaults={
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'email': user.email,
                    'group': group,
                }
            )
            if not created:
                student.group = group
                student.save()
            
            # Создаем тестовые курсы
            courses_data = [
                {'name': 'Введение в программирование', 'code': 'CS101'},
                {'name': 'Базы данных', 'code': 'CS102'},
                {'name': 'Веб-разработка', 'code': 'CS201'},
            ]
            
            courses = []
            for course_data in courses_data:
                course, created = Course.objects.get_or_create(
                    code=course_data['code'],
                    defaults={
                        'name': course_data['name'],
                        'description': f'Описание курса {course_data["name"]}',
                        'credits': 3,
                    }
                )
                courses.append(course)
            
            # Создаем записи на курсы
            for course in courses:
                Enrollment.objects.get_or_create(
                    student=student,
                    course=course,
                )
            
            # Создаем расписание
            schedule_data = [
                {'weekday': 0, 'start_time': '09:00', 'end_time': '10:30', 'course': courses[0]},
                {'weekday': 1, 'start_time': '10:40', 'end_time': '12:10', 'course': courses[1]},
                {'weekday': 2, 'start_time': '13:00', 'end_time': '14:30', 'course': courses[2]},
                {'weekday': 3, 'start_time': '09:00', 'end_time': '10:30', 'course': courses[0]},
                {'weekday': 4, 'start_time': '10:40', 'end_time': '12:10', 'course': courses[1]},
            ]
            
            for sched_data in schedule_data:
                entry, created = ScheduleEntry.objects.get_or_create(
                    course=sched_data['course'],
                    weekday=sched_data['weekday'],
                    start_time=sched_data['start_time'],
                    end_time=sched_data['end_time'],
                    defaults={'classroom': 'Аудитория 101'}
                )
                if created or not entry.groups.filter(id=profile.id).exists():
                    entry.groups.add(profile)
            
            # Создаем тестовые задания (много для демонстрации ИИ)
            from .models import Lecture, Assignment, Submission
            assignments_data = []
            
            # Разные темы для каждого курса
            course_topics_map = {
                courses[0].id: ['Основы Python', 'Переменные и типы', 'Условия и циклы', 'Функции', 'Списки и словари', 'ООП', 'Модули', 'Обработка ошибок', 'Файлы', 'Регулярные выражения'],
                courses[1].id: ['SQL основы', 'SELECT запросы', 'JOIN операции', 'Агрегатные функции', 'Подзапросы', 'Нормализация БД', 'Индексы', 'Транзакции', 'Триггеры', 'Оптимизация'],
                courses[2].id: ['HTML структура', 'CSS стилизация', 'JavaScript основы', 'DOM манипуляции', 'События', 'AJAX', 'JSON', 'LocalStorage', 'Асинхронность', 'Фреймворки'],
            }
            
            for i, course in enumerate(courses):
                topics = course_topics_map.get(course.id, ['Тема 1', 'Тема 2', 'Тема 3'])
                
                # Создаем много заданий разных типов
                course_assignments = []
                
                # Домашние задания (10 штук)
                for hw_num in range(1, 11):
                    topic_idx = (hw_num - 1) % len(topics)
                    course_assignments.append({
                        'title': f'Домашнее задание {hw_num} - {topics[topic_idx]}',
                        'assignment_type': 'homework',
                        'topic': topics[topic_idx],
                        'max_score': 100,
                        'days_offset': -60 + hw_num * 5,  # Распределяем за 60 дней
                    })
                
                # Контрольные работы (5 штук)
                for quiz_num in range(1, 6):
                    topic_idx = (quiz_num * 2 - 1) % len(topics)
                    course_assignments.append({
                        'title': f'Контрольная работа {quiz_num} - {topics[topic_idx]}',
                        'assignment_type': 'quiz',
                        'topic': topics[topic_idx],
                        'max_score': 100,
                        'days_offset': -50 + quiz_num * 8,
                    })
                
                # Лабораторные работы (8 штук)
                for lab_num in range(1, 9):
                    topic_idx = (lab_num * 3 - 2) % len(topics)
                    course_assignments.append({
                        'title': f'Лабораторная работа {lab_num} - {topics[topic_idx]}',
                        'assignment_type': 'lab',
                        'topic': topics[topic_idx],
                        'max_score': 100,
                        'days_offset': -45 + lab_num * 5,
                    })
                
                # Проекты (3 штуки)
                for proj_num in range(1, 4):
                    course_assignments.append({
                        'title': f'Проект {proj_num} - {course.name}',
                        'assignment_type': 'project',
                        'topic': 'Проектирование',
                        'max_score': 100,
                        'days_offset': -30 + proj_num * 10,
                    })
                
                # Итого: 26 заданий на курс
                for ass_data in course_assignments:
                    due_date = timezone.now() + timedelta(days=ass_data['days_offset'])
                    assignment, created = Assignment.objects.get_or_create(
                        course=course,
                        title=ass_data['title'],
                        defaults={
                            'description': f'Подробное описание задания: {ass_data["title"]}. Это задание проверяет понимание темы "{ass_data["topic"]}".',
                            'due_date': due_date,
                            'assignment_type': ass_data['assignment_type'],
                            'topic': ass_data['topic'],
                            'max_score': ass_data['max_score'],
                        }
                    )
                    assignments_data.append((assignment, course))
                for ass_data in course_assignments:
                    due_date = timezone.now() + timedelta(days=ass_data['days_offset'])
                    assignment, created = Assignment.objects.get_or_create(
                        course=course,
                        title=ass_data['title'],
                        defaults={
                            'description': f'Описание задания: {ass_data["title"]}',
                            'due_date': due_date,
                            'assignment_type': ass_data['assignment_type'],
                            'topic': ass_data['topic'],
                            'max_score': ass_data['max_score'],
                        }
                    )
                    assignments_data.append((assignment, course))
            
            # Создаем оценки для тестового студента (много разнообразных для демонстрации ИИ)
            from .models import Grade
            import random
            random.seed(42)  # Для воспроизводимости
            
            # Реалистичные паттерны оценок с трендами для каждого курса
            # Введение в программирование: начинаем хорошо, потом падение, затем восстановление
            python_grades = [88, 92, 85, 78, 82, 90, 88, 95, 92, 89,  # ДЗ
                            85, 80, 88, 82, 90,  # Контрольные
                            90, 85, 88, 92, 87, 89, 91, 88, 90,  # Лабораторные
                            95, 92, 98]  # Проекты
            
            # Базы данных: средние оценки с постепенным улучшением
            db_grades = [72, 68, 70, 75, 73, 78, 75, 80, 78, 82,  # ДЗ
                         65, 70, 72, 75, 78,  # Контрольные
                         70, 72, 75, 78, 80, 82, 85, 83, 88,  # Лабораторные
                         85, 88, 90]  # Проекты
            
            # Веб-разработка: отличные оценки с небольшими колебаниями
            web_grades = [93, 95, 90, 92, 94, 96, 93, 97, 95, 94,  # ДЗ
                          90, 92, 95, 93, 96,  # Контрольные
                          94, 96, 93, 95, 97, 94, 96, 98, 95,  # Лабораторные
                          98, 97, 99]  # Проекты
            
            grade_patterns = {
                courses[0].id: python_grades,
                courses[1].id: db_grades,
                courses[2].id: web_grades,
            }
            
            # Создаем оценки для всех заданий
            assignment_idx = 0
            for assignment, course in assignments_data:
                course_grades = grade_patterns.get(course.id, [75] * 26)
                
                # Берем оценку из паттерна (циклически)
                grade_value = course_grades[assignment_idx % len(course_grades)]
                
                # Добавляем реалистичную вариацию
                variation = random.randint(-2, 2)
                grade_value += variation
                grade_value = max(50, min(100, grade_value))  # Ограничиваем 50-100
                
                # Дата оценки (1-5 дней после дедлайна)
                grade_date = assignment.due_date + timedelta(days=random.randint(1, 5))
                
                # Комментарии в зависимости от оценки
                if grade_value >= 90:
                    comment = f'Отличная работа! Продолжайте в том же духе.'
                elif grade_value >= 80:
                    comment = f'Хорошая работа. Есть небольшие замечания.'
                elif grade_value >= 70:
                    comment = f'Удовлетворительно. Рекомендуется повторить материал.'
                else:
                    comment = f'Требуется дополнительная подготовка по теме "{assignment.topic}".'
                
                # Создаем оценку
                grade, created = Grade.objects.get_or_create(
                    student=user,
                    course=course,
                    assignment=assignment,
                    defaults={
                        'value': grade_value,
                        'topic': assignment.topic,
                        'date': grade_date,
                        'assignment_name': assignment.title,
                        'comment': comment,
                    }
                )
                assignment_idx += 1
            
            # Создаем дополнительные оценки без заданий (промежуточные тесты, активности)
            additional_topics = {
                courses[0].id: ['Основы Python', 'Переменные', 'Циклы', 'Функции', 'ООП', 'Модули', 'Обработка исключений'],
                courses[1].id: ['SQL основы', 'SELECT', 'JOIN', 'Нормализация', 'Индексы', 'Транзакции', 'Оптимизация'],
                courses[2].id: ['HTML', 'CSS', 'JavaScript', 'DOM', 'AJAX', 'JSON', 'Асинхронность'],
            }
            
            additional_grades_data = {
                courses[0].id: [88, 85, 90, 87, 92, 89, 91],  # Python - хорошие оценки
                courses[1].id: [72, 68, 70, 75, 73, 78, 80],  # БД - средние, улучшаются
                courses[2].id: [95, 92, 97, 94, 96, 93, 98],  # Веб - отличные
            }
            
            for course in courses:
                topics = additional_topics.get(course.id, [])
                grades = additional_grades_data.get(course.id, [75] * len(topics))
                
                for i, (topic, grade_val) in enumerate(zip(topics, grades)):
                    days_ago = 70 - i * 5  # Распределяем за последние 70 дней
                    grade_date = timezone.now() - timedelta(days=days_ago)
                    
                    # Добавляем вариацию
                    final_grade = grade_val + random.randint(-2, 2)
                    final_grade = max(50, min(100, final_grade))
                    
                    Grade.objects.get_or_create(
                        student=user,
                        course=course,
                        topic=topic,
                        assignment_name=f'Промежуточный тест: {topic}',
                        defaults={
                            'value': final_grade,
                            'date': grade_date,
                            'comment': f'Проверка знаний по теме "{topic}"',
                        }
                    )
            
            # Создаем посещаемость (реалистичная для демонстрации ИИ)
            from .models import Attendance
            enrollments = Enrollment.objects.filter(student=student)
            
            # Разная посещаемость для разных курсов
            attendance_rates = {
                courses[0].id: 0.92,  # Python - отличная посещаемость
                courses[1].id: 0.78,  # БД - средняя посещаемость
                courses[2].id: 0.95,  # Веб - почти идеальная
            }
            
            # Создаем посещаемость за последние 3 месяца (12 недель)
            for enrollment in enrollments:
                course = enrollment.course
                course_lectures = Lecture.objects.filter(course=course)
                
                # Если лекций еще нет, создадим больше для посещаемости
                if not course_lectures.exists():
                    for i in range(15):  # 15 лекций на курс
                        Lecture.objects.get_or_create(
                            course=course,
                            title=f'Лекция {i+1} - {course.name}',
                            defaults={
                                'content_text': f'Подробное содержание лекции {i+1} по курсу {course.name}. Здесь рассматриваются основные концепции и практические примеры.',
                            }
                        )
                    course_lectures = Lecture.objects.filter(course=course)
                
                attendance_rate = attendance_rates.get(course.id, 0.85)
                
                # Создаем посещаемость за 12 недель
                for lecture in course_lectures[:15]:  # Берем 15 лекций
                    for week in range(12):  # 12 недель назад
                        # Лекции обычно 2 раза в неделю
                        for day_in_week in [0, 3]:  # Понедельник и четверг
                            attendance_date = timezone.now().date() - timedelta(
                                days=week * 7 + day_in_week + random.randint(0, 1)
                            )
                            
                            # Проверяем, не слишком ли старая дата
                            if attendance_date > (timezone.now().date() - timedelta(days=90)):
                                present = random.random() > (1 - attendance_rate)
                                
                                Attendance.objects.get_or_create(
                                    enrollment=enrollment,
                                    lecture=lecture,
                                    date=attendance_date,
                                    defaults={'present': present}
                                )
            
            # Создаем выполнения заданий (submissions) - большинство заданий выполнено
            for assignment, course in assignments_data:
                # 90% заданий выполнено (реалистично)
                if random.random() < 0.90:
                    # Дата сдачи (от 0 до 3 дней до дедлайна - студент сдает вовремя)
                    days_before = random.randint(0, 3)
                    submitted_at = assignment.due_date - timedelta(days=days_before)
                    
                    # Получаем оценку, если есть
                    grade = Grade.objects.filter(student=user, assignment=assignment).first()
                    score = grade.value if grade else None
                    
                    # Текст выполнения зависит от типа задания
                    if assignment.assignment_type == 'homework':
                        text = f'Решение домашнего задания по теме "{assignment.topic}". Выполнены все требования.'
                    elif assignment.assignment_type == 'quiz':
                        text = f'Ответы на контрольную работу по теме "{assignment.topic}".'
                    elif assignment.assignment_type == 'lab':
                        text = f'Отчет по лабораторной работе "{assignment.topic}". Включены код, результаты и выводы.'
                    elif assignment.assignment_type == 'project':
                        text = f'Проект по курсу {course.name}. Включает документацию, код и презентацию.'
                    else:
                        text = f'Выполнение задания: {assignment.title}'
                    
                    Submission.objects.get_or_create(
                        assignment=assignment,
                        student=user,
                        defaults={
                            'text': text,
                            'submitted_at': submitted_at,
                            'score': score,
                        }
                    )
            
            # Создаем тестовые лекции с контентом
            lectures_data = [
                {
                    'course': courses[0],  # Введение в программирование
                    'title': 'Введение в Python',
                    'content_text': '''Python - это высокоуровневый язык программирования общего назначения. Он был создан Гвидо ван Россумом и впервые выпущен в 1991 году.

Основные особенности Python:
- Простой и читаемый синтаксис
- Динамическая типизация
- Интерпретируемый язык
- Кроссплатформенность
- Большая стандартная библиотека

Python используется для веб-разработки, анализа данных, машинного обучения, автоматизации и многого другого. Это отличный язык для начинающих программистов благодаря своей простоте и понятности.

Пример простой программы на Python:
print("Привет, мир!")
x = 10
y = 20
print(f"Сумма: {x + y}")'''
                },
                {
                    'course': courses[0],
                    'title': 'Переменные и типы данных',
                    'content_text': '''В Python переменные создаются простым присваиванием значения. Не нужно объявлять тип переменной заранее.

Основные типы данных:
- Числа (int, float): 10, 3.14
- Строки (str): "Привет", 'Мир'
- Списки (list): [1, 2, 3]
- Словари (dict): {"ключ": "значение"}
- Булевы значения (bool): True, False

Примеры:
age = 25
name = "Иван"
grades = [5, 4, 5, 3]
student = {"имя": "Иван", "возраст": 25}'''
                },
                {
                    'course': courses[0],
                    'title': 'Условия и циклы',
                    'content_text': '''Условные операторы позволяют выполнять код в зависимости от условий.

if-elif-else:
if x > 0:
    print("Положительное")
elif x < 0:
    print("Отрицательное")
else:
    print("Ноль")

Циклы позволяют повторять код:
for i in range(5):
    print(i)

while x < 10:
    x += 1
    print(x)'''
                },
                {
                    'course': courses[1],  # Базы данных
                    'title': 'Введение в SQL',
                    'content_text': '''SQL (Structured Query Language) - язык для работы с реляционными базами данных.

Основные команды:
- SELECT - выборка данных
- INSERT - вставка данных
- UPDATE - обновление данных
- DELETE - удаление данных
- CREATE TABLE - создание таблицы

Пример SELECT:
SELECT имя, фамилия FROM студенты WHERE группа = 'CS-101';

Пример INSERT:
INSERT INTO студенты (имя, фамилия, группа) 
VALUES ('Иван', 'Иванов', 'CS-101');'''
                },
                {
                    'course': courses[1],
                    'title': 'Нормализация баз данных',
                    'content_text': '''Нормализация - процесс организации данных в базе для уменьшения избыточности.

Основные нормальные формы:
1NF - каждая ячейка содержит одно значение
2NF - 1NF + нет частичных зависимостей
3NF - 2NF + нет транзитивных зависимостей

Преимущества нормализации:
- Уменьшение дублирования данных
- Улучшение целостности данных
- Упрощение обновлений
- Экономия места'''
                },
                {
                    'course': courses[1],
                    'title': 'JOIN операции',
                    'content_text': '''JOIN позволяет объединять данные из нескольких таблиц.

Типы JOIN:
- INNER JOIN - только совпадающие записи
- LEFT JOIN - все записи из левой таблицы
- RIGHT JOIN - все записи из правой таблицы
- FULL OUTER JOIN - все записи из обеих таблиц

Пример:
SELECT студенты.имя, курсы.название
FROM студенты
INNER JOIN записи ON студенты.id = записи.студент_id
INNER JOIN курсы ON записи.курс_id = курсы.id;'''
                },
                {
                    'course': courses[2],  # Веб-разработка
                    'title': 'HTML основы',
                    'content_text': '''HTML (HyperText Markup Language) - язык разметки для создания веб-страниц.

Основные теги:
- <html> - корневой элемент
- <head> - метаинформация
- <body> - содержимое страницы
- <h1>-<h6> - заголовки
- <p> - параграф
- <a> - ссылка
- <img> - изображение
- <div> - контейнер

Пример:
<!DOCTYPE html>
<html>
<head>
    <title>Моя страница</title>
</head>
<body>
    <h1>Привет, мир!</h1>
    <p>Это моя первая веб-страница.</p>
</body>
</html>'''
                },
                {
                    'course': courses[2],
                    'title': 'CSS стилизация',
                    'content_text': '''CSS (Cascading Style Sheets) - язык для стилизации HTML элементов.

Способы подключения CSS:
1. Внутренний стиль: <style>...</style>
2. Внешний файл: <link rel="stylesheet" href="style.css">
3. Инлайн: <div style="color: red;">

Основные свойства:
- color - цвет текста
- background-color - цвет фона
- font-size - размер шрифта
- margin - внешние отступы
- padding - внутренние отступы
- border - граница

Пример:
h1 {
    color: blue;
    font-size: 24px;
    margin: 20px;
}'''
                },
                {
                    'course': courses[2],
                    'title': 'JavaScript основы',
                    'content_text': '''JavaScript - язык программирования для создания интерактивных веб-страниц.

Основные концепции:
- Переменные: let, const, var
- Функции: function, arrow functions
- Объекты и массивы
- DOM манипуляции
- События

Пример:
let name = "Иван";
function greet(name) {
    console.log("Привет, " + name + "!");
}
greet(name);

// Работа с DOM
document.getElementById("myButton").addEventListener("click", function() {
    alert("Кнопка нажата!");
});'''
                },
            ]
            
            for lecture_data in lectures_data:
                lecture, created = Lecture.objects.get_or_create(
                    course=lecture_data['course'],
                    title=lecture_data['title'],
                    defaults={
                        'content_text': lecture_data['content_text'],
                    }
                )
            
            return render(request, 'main/create_test_student_success.html', {
                'username': username,
                'password': password,
            })
        except Exception as e:
            return render(request, 'main/create_test_student_error.html', {
                'error': str(e),
            })
    
    return render(request, 'main/create_test_student.html')

def create_test_teacher_view(request):
    """Создает тестового преподавателя и базовые учебные данные для демо."""
    if not request.user.is_authenticated or not request.user.is_staff:
        messages.error(request, 'Доступ запрещён.')
        return redirect('login')

    if request.method == 'POST':
        try:
            username = 'test_teacher'
            password = 'teacher123456'

            teacher_user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': 'test_teacher@uniquest.kz',
                    'first_name': 'Тестовый',
                    'last_name': 'Преподаватель',
                    'is_staff': True,
                }
            )
            if created:
                teacher_user.set_password(password)
                teacher_user.save()

            Profile.objects.get_or_create(
                user=teacher_user,
                defaults={'role': Profile.ROLE_TEACHER}
            )

            # Используем те же демо-курсы, что и для студента.
            courses_data = [
                {'name': 'Введение в программирование', 'code': 'CS101'},
                {'name': 'Базы данных', 'code': 'CS102'},
                {'name': 'Веб-разработка', 'code': 'CS201'},
            ]

            courses = []
            for course_data in courses_data:
                course, _ = Course.objects.get_or_create(
                    code=course_data['code'],
                    defaults={
                        'name': course_data['name'],
                        'description': f'Описание курса {course_data["name"]}',
                        'credits': 3,
                    }
                )
                if course.teacher_id != teacher_user.id:
                    course.teacher = teacher_user
                    course.save(update_fields=['teacher'])
                courses.append(course)

            messages.success(
                request,
                f'Тестовый преподаватель готов: логин "{username}", пароль "{password}". '
                f'Курсов привязано: {len(courses)}.'
            )
        except Exception as e:
            messages.error(request, f'Ошибка при создании преподавателя: {e}')

    return render(request, 'main/create_test_teacher.html')

@ensure_csrf_cookie
def register_view(request):
    if request.user.is_authenticated:
        return redirect(_role_home(request.user))

    _ensure_registration_reference_data()

    if request.method == 'POST':
        if _rate_limit_exceeded(request, "register", limit=8, window_seconds=300):
            messages.error(request, "Слишком много попыток регистрации. Повторите позже.")
            form = UserRegisterForm(request.POST)
            return render(request, 'main/register.html', {'form': form})
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            try:
                user = form.save()
                role = form.cleaned_data['role']
                group = form.cleaned_data.get('group')
                specialty = form.cleaned_data.get('specialty')

                # Профиль может быть создан сигналом автоматически: обновляем существующий.
                Profile.objects.update_or_create(
                    user=user,
                    defaults={
                        'role': role,
                        'group': group if role == Profile.ROLE_STUDENT else None,
                        'specialty': specialty if role == Profile.ROLE_STUDENT else None,
                        'enrollment_date': timezone.now().date()
                        if role == Profile.ROLE_STUDENT
                        else None,
                    },
                )
                # Создаём сущность Student для студентов, чтобы связать с академическими моделями
                if role == Profile.ROLE_STUDENT:
                    # Генерируем уникальный email если он уже существует
                    student_email = user.email or f"{user.username}@example.com"
                    email_base = student_email.split('@')[0]
                    email_domain = student_email.split('@')[1] if '@' in student_email else 'example.com'
                    counter = 1
                    while Student.objects.filter(email=student_email).exists():
                        student_email = f"{email_base}{counter}@{email_domain}"
                        counter += 1
                    
                    Student.objects.get_or_create(
                        user=user,
                        defaults={
                            "first_name": user.first_name or user.username,
                            "last_name": user.last_name or "",
                            "email": student_email,
                            "group": group,
                        },
                    )
                login(request, user)
                messages.success(request, 'Регистрация успешна. Добро пожаловать!')
                return redirect("teacher_dashboard" if role == Profile.ROLE_TEACHER else "dashboard")
            except Exception as e:
                messages.error(request, f'Ошибка при регистрации: {str(e)}')
                # Логируем ошибку для отладки
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f'Registration error: {str(e)}', exc_info=True)
    else:
        form = UserRegisterForm()
    return render(request, 'main/register.html', {'form': form})

def setup_demo_admin_view(request):
    """Одноразовая настройка admin на Render (Shell недоступен)."""
    if not getattr(settings, "ENSURE_DEMO_ADMIN", False):
        raise Http404
    try:
        from main.bootstrap import ensure_default_admin
        from main.bootstrap import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME

        ensure_default_admin()
        return JsonResponse(
            {
                "ok": True,
                "username": DEFAULT_ADMIN_USERNAME,
                "password": DEFAULT_ADMIN_PASSWORD,
                "login_url": reverse("login"),
            }
        )
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect(_role_home(request.user))

    if getattr(settings, "ENSURE_DEMO_ADMIN", False):
        try:
            from main.bootstrap import ensure_default_admin

            ensure_default_admin()
        except Exception:
            pass

    if request.method == 'POST':
        if _rate_limit_exceeded(request, "login", limit=20, window_seconds=300):
            messages.error(request, 'Слишком много попыток входа. Повторите позже.')
            form = AuthenticationForm(request=request, data=request.POST)
            return render(request, 'main/login.html', {'form': form})
        form = AuthenticationForm(request=request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, 'Вы успешно вошли.')
            return redirect(_role_home(user))
        else:
            messages.error(request, 'Ошибка входа. Проверьте логин и пароль.')
    else:
        form = AuthenticationForm()
    return render(request, 'main/login.html', {'form': form})

def logout_view(request):
    logout(request)
    messages.info(request, 'Вы вышли из аккаунта.')
    return redirect('index')

# ===== Декораторы доступа =====
def teacher_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'profile') or request.user.profile.role != Profile.ROLE_TEACHER:
            messages.error(request, 'Доступ только для преподавателей.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped

def student_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'profile') or request.user.profile.role != Profile.ROLE_STUDENT:
            messages.error(request, 'Доступ только для студентов.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped


def staff_required(view_func):
    return user_passes_test(lambda u: u.is_authenticated and u.is_staff)(view_func)


def _compute_grade_trend(values):
    if len(values) < 3:
        return 0.0
    # Линейный тренд: итоговое изменение балла по траектории.
    n = len(values)
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = 0.0
    denominator = 0.0
    for idx, value in enumerate(values):
        dx = idx - x_mean
        numerator += dx * (value - y_mean)
        denominator += dx * dx
    if denominator == 0:
        return 0.0
    slope = numerator / denominator
    return slope * (n - 1)


def _compute_stddev(values):
    if len(values) < 2:
        return 0.0
    mean_value = sum(values) / len(values)
    variance = sum((value - mean_value) ** 2 for value in values) / len(values)
    return variance ** 0.5


def build_student_performance_report(student_user, teacher=None):
    student_obj = Student.objects.filter(user=student_user).first()
    if not student_obj:
        return {
            "student_obj": None,
            "overall_avg": 0.0,
            "overall_attendance": 0.0,
            "overall_submission_rate": 0.0,
            "overall_risk_level": "low",
            "overall_risk_score": 0,
            "course_reports": [],
            "risk_triggers": [],
            "top_strengths": [],
            "top_weaknesses": [],
            "threats": [],
            "recommendations": ["Недостаточно данных для аналитики. Обратитесь к куратору/преподавателю."],
        }

    enrollments = Enrollment.objects.filter(student=student_obj).select_related("course")
    if teacher:
        enrollments = enrollments.filter(course__teacher=teacher)

    course_reports = []
    risk_triggers = []
    strong_topics = defaultdict(list)
    weak_topics = defaultdict(list)

    all_grade_values = []
    weighted_grade_sum = 0.0
    weighted_grade_count = 0
    total_attendance_records = 0
    total_attended = 0
    total_assignments = 0
    total_submissions = 0
    weighted_risk_sum = 0.0
    weight_total = 0.0

    for enrollment in enrollments:
        course = enrollment.course
        grades = list(
            Grade.objects.filter(student=student_user, course=course).order_by("date")
        )
        teacher_comments = [
            row.comment.strip()
            for row in Grade.objects.filter(student=student_user, course=course)
            .exclude(comment__isnull=True)
            .exclude(comment__exact="")
            .order_by("-date")[:5]
            if row.comment and row.comment.strip()
        ]
        grade_values = [float(g.value) for g in grades]
        avg_grade = (sum(grade_values) / len(grade_values)) if grade_values else 0.0
        recent_slice = grade_values[-3:] if grade_values else []
        recent_avg = (sum(recent_slice) / len(recent_slice)) if recent_slice else avg_grade
        trend = _compute_grade_trend(grade_values)
        grade_std = _compute_stddev(grade_values)
        consistency_index = max(0.0, 100.0 - min(100.0, grade_std * 6.0))
        all_grade_values.extend(grade_values)
        weighted_grade_sum += sum(grade_values) * max(1, course.credits)
        weighted_grade_count += len(grade_values) * max(1, course.credits)

        attendance_qs = Attendance.objects.filter(enrollment=enrollment).order_by("date")
        att_total = attendance_qs.count()
        att_present = attendance_qs.filter(present=True).count()
        attendance_rate = (att_present * 100 / att_total) if att_total else 0.0
        attendance_values = [1 if row.present else 0 for row in attendance_qs]
        recent_att_window = attendance_values[-4:] if attendance_values else []
        prev_att_window = attendance_values[:-4] if len(attendance_values) > 4 else []
        recent_attendance = (
            sum(recent_att_window) * 100 / len(recent_att_window)
            if recent_att_window
            else attendance_rate
        )
        prev_attendance = (
            sum(prev_att_window) * 100 / len(prev_att_window)
            if prev_att_window
            else attendance_rate
        )
        attendance_trend = recent_attendance - prev_attendance
        total_attendance_records += att_total
        total_attended += att_present

        assignments_due_qs = Assignment.objects.filter(course=course, due_date__lte=timezone.now())
        assignments_total = assignments_due_qs.count()
        if assignments_total == 0:
            assignments_total = Assignment.objects.filter(course=course).count()

        submissions_qs = Submission.objects.filter(
            student=student_user, assignment__course=course
        ).select_related("assignment")
        submissions_total = submissions_qs.count()
        submission_rate = (submissions_total * 100 / assignments_total) if assignments_total else 100.0
        on_time_submissions = sum(
            1
            for submission in submissions_qs
            if submission.assignment and submission.submitted_at <= submission.assignment.due_date
        )
        on_time_rate = (on_time_submissions * 100 / submissions_total) if submissions_total else 100.0
        total_assignments += assignments_total
        total_submissions += submissions_total

        topic_stats = (
            Grade.objects.filter(student=student_user, course=course)
            .exclude(topic__isnull=True)
            .exclude(topic__exact="")
            .values("topic")
            .annotate(avg_topic=Avg("value"), topic_items=Count("id"))
            .order_by("-avg_topic")
        )
        course_strengths = []
        course_weaknesses = []
        for row in topic_stats:
            topic_name = row["topic"]
            topic_avg = float(row["avg_topic"] or 0)
            topic_items = int(row.get("topic_items") or 0)
            if topic_avg >= 82 and topic_items >= 2:
                strong_topics[topic_name].append(topic_avg)
                course_strengths.append({"topic": topic_name, "avg": topic_avg})
            elif topic_avg <= 72 and topic_items >= 2:
                weak_topics[topic_name].append(topic_avg)
                course_weaknesses.append({"topic": topic_name, "avg": topic_avg})

        risk_score = 0.0
        indicators = []
        success_signals = []

        if avg_grade < 60:
            risk_score += 34
            indicators.append("Критически низкий средний балл")
        elif avg_grade < 70:
            risk_score += 22
            indicators.append("Средний балл ниже академической нормы")
        elif avg_grade < 80:
            risk_score += 10

        if recent_avg + 3 < avg_grade:
            risk_score += 12
            indicators.append("Последние результаты ниже базовой траектории")
        elif recent_avg > avg_grade + 3:
            success_signals.append("Наблюдается ускорение академического прогресса")

        if attendance_rate < 60:
            risk_score += 28
            indicators.append("Критически низкая посещаемость (САПА)")
        elif attendance_rate < 75:
            risk_score += 17
            indicators.append("Посещаемость ниже целевого уровня")
        elif attendance_rate < 85:
            risk_score += 7
        else:
            success_signals.append("Стабильная высокая посещаемость")

        if attendance_trend < -12:
            risk_score += 10
            indicators.append("Снижение посещаемости в последние недели")

        if submission_rate < 60:
            risk_score += 22
            indicators.append("Низкая доля сданных заданий")
        elif submission_rate < 80:
            risk_score += 12
            indicators.append("Нестабильная сдача заданий")
        elif submission_rate >= 95:
            success_signals.append("Почти полное выполнение учебных заданий")

        if on_time_rate < 60:
            risk_score += 12
            indicators.append("Большая часть работ сдаётся с опозданием")
        elif on_time_rate < 80:
            risk_score += 6
            indicators.append("Требуется повышение дисциплины дедлайнов")

        if trend < -12:
            risk_score += 24
            indicators.append("Сильный нисходящий тренд оценок")
        elif trend < -6:
            risk_score += 14
            indicators.append("Негативный тренд оценок")
        elif trend < -3:
            risk_score += 7
        elif trend > 6:
            success_signals.append("Выраженный положительный тренд оценок")

        if grade_std > 15:
            risk_score += 14
            indicators.append("Высокая нестабильность результатов")
        elif grade_std > 10:
            risk_score += 8
            indicators.append("Нестабильность оценок между контрольными точками")
        elif len(grade_values) >= 5:
            success_signals.append("Стабильная успеваемость без резких провалов")

        if len(grade_values) < 4:
            risk_score += 8
            indicators.append("Недостаточно оценок для уверенного прогноза")
        if att_total == 0:
            risk_score += 6
            indicators.append("Недостаточно данных посещаемости")

        if (
            avg_grade >= 88
            and attendance_rate >= 90
            and submission_rate >= 90
            and on_time_rate >= 85
            and trend >= 2
        ):
            risk_score -= 12
            success_signals.append("Комплексно сильная академическая траектория")

        risk_score = max(0, min(100, int(round(risk_score))))
        data_confidence = min(
            100.0,
            (
                min(len(grade_values), 8) / 8 * 40
                + min(att_total, 10) / 10 * 30
                + min(assignments_total, 8) / 8 * 30
            ),
        )

        if risk_score >= 75:
            risk_level = "critical"
        elif risk_score >= 50:
            risk_level = "high"
        elif risk_score >= 25:
            risk_level = "medium"
        else:
            risk_level = "low"

        if indicators:
            risk_triggers.append({"course": course, "items": indicators, "level": risk_level})
        if teacher_comments:
            risk_triggers.append(
                {
                    "course": course,
                    "items": [f"Комментарий преподавателя: {text}" for text in teacher_comments[:2]],
                    "level": risk_level,
                }
            )

        course_reports.append(
            {
                "course": course,
                "avg_grade": avg_grade,
                "attendance_rate": attendance_rate,
                "submission_rate": submission_rate,
                "on_time_rate": on_time_rate,
                "trend": trend,
                "recent_avg": recent_avg,
                "consistency_index": consistency_index,
                "attendance_trend": attendance_trend,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "risk_indicators": indicators,
                "success_signals": success_signals,
                "data_confidence": data_confidence,
                "strengths": course_strengths[:3],
                "weaknesses": course_weaknesses[:3],
                "teacher_comments": teacher_comments,
            }
        )
        course_weight = max(1.0, float(course.credits or 1))
        weighted_risk_sum += risk_score * course_weight
        weight_total += course_weight

    course_reports.sort(key=lambda item: item["risk_score"], reverse=True)

    overall_avg = (
        weighted_grade_sum / weighted_grade_count
        if weighted_grade_count
        else ((sum(all_grade_values) / len(all_grade_values)) if all_grade_values else 0.0)
    )
    overall_attendance = (total_attended * 100 / total_attendance_records) if total_attendance_records else 0.0
    overall_submission_rate = (total_submissions * 100 / total_assignments) if total_assignments else 100.0
    overall_risk_score = int(weighted_risk_sum / weight_total) if weight_total else 0

    if overall_risk_score >= 75:
        overall_risk_level = "critical"
    elif overall_risk_score >= 50:
        overall_risk_level = "high"
    elif overall_risk_score >= 25:
        overall_risk_level = "medium"
    else:
        overall_risk_level = "low"

    top_strengths = sorted(
        [{"topic": topic, "avg": sum(vals) / len(vals)} for topic, vals in strong_topics.items()],
        key=lambda row: row["avg"],
        reverse=True,
    )[:5]
    top_weaknesses = sorted(
        [{"topic": topic, "avg": sum(vals) / len(vals)} for topic, vals in weak_topics.items()],
        key=lambda row: row["avg"],
    )[:5]

    threats = [item for item in course_reports if item["risk_level"] in ("critical", "high")]
    recommendations = []
    if overall_attendance < 80:
        recommendations.append("Повысить посещаемость до уровня не ниже 80%.")
    if overall_submission_rate < 85:
        recommendations.append("Стабилизировать сдачу заданий и исключить пропуски дедлайнов.")
    high_risk_courses = [row for row in course_reports if row["risk_level"] in ("critical", "high")]
    if high_risk_courses:
        priorities = ", ".join([row["course"].name for row in high_risk_courses[:2]])
        recommendations.append(
            f"Сфокусировать индивидуальную поддержку на дисциплинах: {priorities}."
        )
    unstable_courses = [row for row in course_reports if row["consistency_index"] < 45]
    if unstable_courses:
        recommendations.append(
            "Снизить разброс результатов: добавить еженедельные мини-контроли и короткие консультации."
        )
    if top_weaknesses:
        weak_topics_list = ", ".join([row["topic"] for row in top_weaknesses[:3]])
        recommendations.append(f"Приоритетно проработать темы: {weak_topics_list}.")
    if not recommendations:
        recommendations.append("Траектория стабильна. Сфокусируйтесь на закреплении сильных тем и экзаменационной подготовке.")

    return {
        "student_obj": student_obj,
        "overall_avg": overall_avg,
        "overall_attendance": overall_attendance,
        "overall_submission_rate": overall_submission_rate,
        "overall_risk_level": overall_risk_level,
        "overall_risk_score": overall_risk_score,
        "course_reports": course_reports,
        "risk_triggers": risk_triggers,
        "top_strengths": top_strengths,
        "top_weaknesses": top_weaknesses,
        "threats": threats,
        "recommendations": recommendations,
    }

# ===== Панель пользователя =====
def _dashboard_impl(request):
    user = request.user
    
    # Проверяем роль преподавателя ПЕРВЫМ делом
    if hasattr(user, 'profile') and user.profile.role == Profile.ROLE_TEACHER:
        return redirect('teacher_dashboard')
    
    # Стандартные роли - только для администраторов
    if user.is_staff:
        # Возможные действия администратора
        from django.core.management import call_command

        if request.method == "POST":
            action = request.POST.get("action")
            if action == "seed_demo":
                call_command("seed_demo", students=500, groups=20, courses=30, seed=42)
                messages.success(request, "Демо-данные успешно сгенерированы.")
            elif action == "train_model":
                call_command("train_grade_model", save_path="models/grade_model.pkl")
                messages.success(request, "Модель прогноза оценок обучена.")
            elif action == "index_lectures":
                call_command("index_lectures")
                messages.success(request, "Индексация лекций выполнена.")
            return redirect("dashboard")

        # Админский дашборд с общей статистикой и KPI
        total_students = Student.objects.count()
        active_groups = Group.objects.count()
        total_courses = Course.objects.count()
        recent_enrollments = Enrollment.objects.select_related(
            "student", "course"
        ).order_by("-enrolled_at")[:10]

        total_grades = Grade.objects.count()
        avg_grade_system = Grade.objects.aggregate(avg=Avg("value"))["avg"] or 0

        attendance_total = Attendance.objects.count()
        attendance_present = Attendance.objects.filter(present=True).count()
        attendance_system = (attendance_present * 100 / attendance_total) if attendance_total else 0

        # Риск-оценка по студентам и группам
        risk_students = []
        group_risk_map = {}
        students = Student.objects.select_related("group", "user")
        for st in students:
            enrollments_st = Enrollment.objects.filter(student=st)
            # Не завязываемся на Grade.enrollment: в части прод-БД это поле может отсутствовать
            # при старой схеме, что приводит к 500 на /dashboard/.
            student_user = getattr(st, "user", None)
            course_ids = list(enrollments_st.values_list("course_id", flat=True))
            if student_user and course_ids:
                grades_qs = Grade.objects.filter(
                    student=student_user,
                    course_id__in=course_ids,
                )
            else:
                grades_qs = Grade.objects.none()
            att_qs = Attendance.objects.filter(enrollment__in=enrollments_st)

            st_avg = grades_qs.aggregate(avg=Avg("value"))["avg"] or 0
            st_total_att = att_qs.count()
            st_present_att = att_qs.filter(present=True).count()
            st_attendance = (st_present_att * 100 / st_total_att) if st_total_att else 0

            risk_score = 0
            triggers = []
            if st_avg < 65:
                risk_score += 50
                triggers.append("Низкая успеваемость")
            if st_attendance < 70:
                risk_score += 50
                triggers.append("Низкая посещаемость")

            if risk_score > 0:
                risk_students.append(
                    {
                        "student": st,
                        "avg_grade": st_avg,
                        "attendance_rate": st_attendance,
                        "risk_score": risk_score,
                        "triggers": triggers,
                    }
                )

            if st.group:
                g_name = st.group.name
                if g_name not in group_risk_map:
                    group_risk_map[g_name] = {"group": st.group, "students": 0, "risk_count": 0}
                group_risk_map[g_name]["students"] += 1
                if risk_score > 0:
                    group_risk_map[g_name]["risk_count"] += 1

        risk_students = sorted(risk_students, key=lambda x: x["risk_score"], reverse=True)[:10]
        risk_groups = sorted(
            [
                {
                    "group": v["group"],
                    "students": v["students"],
                    "risk_count": v["risk_count"],
                    "risk_percent": (v["risk_count"] * 100 / v["students"]) if v["students"] else 0,
                }
                for v in group_risk_map.values()
            ],
            key=lambda x: x["risk_percent"],
            reverse=True,
        )[:8]

        return render(
            request,
            "main/dashboard.html",
            {
                "is_admin_dashboard": True,
                "total_students": total_students,
                "active_groups": active_groups,
                "total_courses": total_courses,
                "recent_enrollments": recent_enrollments,
                "total_grades": total_grades,
                "avg_grade_system": avg_grade_system,
                "attendance_system": attendance_system,
                "risk_students": risk_students,
                "risk_groups": risk_groups,
            },
        )

    # Для студентов (личный кабинет)
    # Получаем курсы, на которые записан студент
    student_profile = getattr(user, 'profile', None)
    if student_profile and student_profile.role == Profile.ROLE_STUDENT:
        # Получаем студента из модели Student
        try:
            student_obj = Student.objects.get(user=user)
            enrollments = Enrollment.objects.filter(student=student_obj).select_related('course')
            courses = [enrollment.course for enrollment in enrollments]
        except Student.DoesNotExist:
            courses = Course.objects.all()[:10]  # Fallback
    else:
        courses = Course.objects.all()[:10]
    
    user_grades = Grade.objects.filter(student=user).select_related('course')
    avg_score = user_grades.aggregate(avg=Avg('value'))['avg'] or 0

    recent_grades = user_grades[:5]
    upcoming_assignments = Assignment.objects.filter(
        course__in=courses,
        due_date__gte=timezone.now()
    ).order_by('due_date')[:5]
    course_ids = [c.id for c in courses if c]
    quiz_qs = LectureQuiz.objects.filter(
        course_id__in=course_ids,
        is_active=True,
    ).select_related("course", "assignment").order_by("-created_at")
    quiz_attempts = {
        a.quiz_id: a
        for a in LectureQuizAttempt.objects.filter(quiz__in=quiz_qs, student=user).order_by("-submitted_at")
    }
    upcoming_quizzes = []
    for quiz in quiz_qs[:12]:
        attempt = quiz_attempts.get(quiz.id)
        attempts_used = LectureQuizAttempt.objects.filter(quiz=quiz, student=user).count()
        attempts_left = max(0, quiz.max_attempts - attempts_used)
        upcoming_quizzes.append(
            {
                "quiz": quiz,
                "attempt": attempt,
                "attempts_left": attempts_left,
                "is_available": attempts_left > 0,
            }
        )
    recent_documents = list(
        _recent_materials_queryset(Lecture.objects.filter(course__in=courses))[:10]
    )

    return render(request, 'main/dashboard.html', {
        'courses': courses,
        'user_grades': user_grades,
        'avg_score': avg_score,
        'recent_grades': recent_grades,
        'upcoming_assignments': upcoming_assignments,
        'upcoming_quizzes': upcoming_quizzes,
        'recent_documents': recent_documents,
        'is_admin_dashboard': False,
    })


@login_required
def dashboard(request):
    """
    Защитный wrapper для продакшена: не отдаём 500 пользователю,
    даже если в аналитике возникла неожиданная ошибка данных.
    """
    try:
        return _dashboard_impl(request)
    except Exception:
        import logging

        logger = logging.getLogger(__name__)
        logger.exception("Dashboard failed, fallback response returned")
        messages.warning(
            request,
            "Некоторые аналитические блоки временно недоступны. Показан упрощённый дашборд."
        )
        if request.user.is_staff:
            return render(
                request,
                "main/dashboard.html",
                {
                    "is_admin_dashboard": True,
                    "total_students": 0,
                    "active_groups": 0,
                    "total_courses": 0,
                    "recent_enrollments": [],
                    "total_grades": 0,
                    "avg_grade_system": 0,
                    "attendance_system": 0,
                    "risk_students": [],
                    "risk_groups": [],
                },
            )

        return render(
            request,
            "main/dashboard.html",
            {
                "courses": [],
                "user_grades": [],
                "avg_score": 0,
                "recent_grades": [],
                "upcoming_assignments": [],
                "upcoming_quizzes": [],
                "recent_documents": [],
                "is_admin_dashboard": False,
            },
        )


@login_required
@student_required
def student_passport_view(request):
    """Паспорт студента: глубокий анализ сильных/слабых сторон и рисков."""
    report = build_student_performance_report(request.user)
    trajectory = [
        {
            "course": item["course"],
            "avg_grade": item["avg_grade"],
            "attendance_rate": item["attendance_rate"],
            "trend": item["trend"],
            "submission_rate": item["submission_rate"],
            "risk_level": item["risk_level"],
        }
        for item in report["course_reports"]
    ]
    selected_course_id = request.GET.get("course_id", "").strip()
    selected_course_report = None
    selected_course_grades = Grade.objects.none()
    if selected_course_id.isdigit():
        selected_course_report = next(
            (item for item in report["course_reports"] if item["course"].id == int(selected_course_id)),
            None,
        )
        if selected_course_report:
            selected_course_grades = Grade.objects.filter(
                student=request.user,
                course=selected_course_report["course"],
            ).order_by("-date")

    return render(
        request,
        "main/student_passport.html",
        {
            "student_obj": report["student_obj"],
            "overall_avg": report["overall_avg"],
            "overall_attendance": report["overall_attendance"],
            "overall_submission_rate": report["overall_submission_rate"],
            "overall_risk_level": report["overall_risk_level"],
            "overall_risk_score": report["overall_risk_score"],
            "trajectory": trajectory,
            "risk_triggers": report["risk_triggers"],
            "top_strengths": report["top_strengths"],
            "top_weaknesses": report["top_weaknesses"],
            "threats": report["threats"],
            "recommendations": report["recommendations"],
            "selected_course_id": selected_course_id,
            "selected_course_report": selected_course_report,
            "selected_course_grades": selected_course_grades,
        },
    )

@login_required
def course_detail(request, pk):
    course = get_object_or_404(Course, pk=pk)
    user_profile = getattr(request.user, "profile", None)
    if request.user.is_staff and not user_profile:
        user_profile = None

    if request.user.is_staff and not user_profile:
        pass
    elif user_profile and user_profile.role == Profile.ROLE_TEACHER:
        if course.teacher_id != request.user.id:
            messages.error(request, "Доступ к курсу разрешён только его преподавателю.")
            return redirect("teacher_dashboard")
    else:
        has_access = Enrollment.objects.filter(student__user=request.user, course=course).exists()
        if not has_access:
            messages.error(request, "Доступ к курсу разрешён только записанным студентам.")
            return redirect("dashboard")

    assignments = Assignment.objects.filter(course=course)
    recommendations = Recommendation.objects.filter(submission__assignment__in=assignments)
    schedule = ScheduleEntry.objects.filter(course=course).order_by('weekday', 'start_time')
    lectures = Lecture.objects.filter(course=course).order_by("-created_at")
    quizzes = LectureQuiz.objects.filter(course=course, is_active=True).select_related("assignment", "lecture")
    
    # Получаем оценки для текущего пользователя (если студент) или все (если преподаватель)
    if user_profile and user_profile.role == Profile.ROLE_TEACHER:
        grades = Grade.objects.filter(course=course).select_related('student').order_by('-date')
        student_attempts = {}
    else:
        grades = Grade.objects.filter(course=course, student=request.user).order_by('-date')
        recommendations = recommendations.filter(submission__student=request.user)
        attempts = LectureQuizAttempt.objects.filter(quiz__in=quizzes, student=request.user).select_related("quiz").order_by("-submitted_at")
        student_attempts = {}
        for attempt in attempts:
            if attempt.quiz_id not in student_attempts:
                student_attempts[attempt.quiz_id] = float(attempt.score)
    
    return render(request, 'main/course_detail.html', {
        'course': course,
        'assignments': assignments,
        'recommendations': recommendations,
        'schedule': schedule,
        'grades': grades,
        'lectures': lectures,
        'quizzes': quizzes,
        'student_attempts': student_attempts,
        'is_teacher_view': bool(user_profile and user_profile.role == Profile.ROLE_TEACHER),
    })

@login_required
def profile_view(request):
    Profile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        u_form = UserUpdateForm(request.POST, instance=request.user)
        p_form = ProfileUpdateForm(
            request.POST,
            request.FILES,
            instance=request.user.profile,
            user=request.user,
        )
        if u_form.is_valid() and p_form.is_valid():
            u_form.save()
            p_form.save()
            messages.success(request, 'Профиль обновлён.')
            return redirect('profile')
    else:
        u_form = UserUpdateForm(instance=request.user)
        p_form = ProfileUpdateForm(instance=request.user.profile, user=request.user)
    return render(request, 'main/profile.html', {'u_form': u_form, 'p_form': p_form})

@login_required
@student_required
def schedule_view(request):
    user = request.user
    conditions = Q(groups__user=user)
    profile = getattr(user, "profile", None)
    if profile and profile.group:
        # Получаем профили студентов этой группы
        group_profiles = Profile.objects.filter(group=profile.group, role=Profile.ROLE_STUDENT)
        conditions |= Q(groups__in=group_profiles)
    schedule = (
        ScheduleEntry.objects.filter(conditions)
        .select_related("course")
        .distinct()
        .order_by("weekday", "start_time")
    )
    days = [
        (0, "Понедельник"),
        (1, "Вторник"),
        (2, "Среда"),
        (3, "Четверг"),
        (4, "Пятница"),
        (5, "Суббота"),
    ]
    time_slots = sorted({(str(item.start_time), str(item.end_time)) for item in schedule})
    grid = []
    for start, end in time_slots:
        row = {"time": f"{start} - {end}", "cells": []}
        for day_idx, _ in days:
            items = [
                e for e in schedule
                if e.weekday == day_idx and str(e.start_time) == start and str(e.end_time) == end
            ]
            row["cells"].append(items)
        grid.append(row)

    return render(
        request,
        'main/schedule.html',
        {'schedule': schedule, 'days': days, 'grid': grid},
    )

@login_required
@student_required
def grades_view(request):
    user = request.user
    grades = Grade.objects.filter(student=user).select_related('course', 'assignment').order_by('-date')
    selected_course_id = request.GET.get("course_id", "").strip()
    total_grades = grades.count()
    avg_score = grades.aggregate(avg=Avg('value'))['avg'] or 0
    courses_stats = {}

    for grade in grades:
        if grade.course.id not in courses_stats:
            courses_stats[grade.course.id] = {
                'course': grade.course,
                'grades': [],
                'avg': 0,
            }
        courses_stats[grade.course.id]['grades'].append(grade)

    for course_id, stats in courses_stats.items():
        numeric = [float(g.value) for g in stats['grades']]
        stats['avg'] = (sum(numeric) / len(numeric)) if numeric else 0
        stats['grades'] = sorted(stats['grades'], key=lambda g: g.date, reverse=True)

    weekday_map = {
        0: "Понедельник",
        1: "Вторник",
        2: "Среда",
        3: "Четверг",
        4: "Пятница",
        5: "Суббота",
        6: "Воскресенье",
    }

    all_grades_rows = []
    for g in grades:
        all_grades_rows.append(
            {
                "grade": g,
                "weekday": weekday_map.get(g.date.weekday(), "—") if g.date else "—",
            }
        )

    selected_course = None
    selected_course_grades = Grade.objects.none()
    selected_course_rows = []
    calculator_defaults = {
        "rk1": 70.0,
        "rk2": 70.0,
        "exam": 70.0,
        "has_factual_data": False,
    }
    if selected_course_id.isdigit() and int(selected_course_id) in courses_stats:
        selected_course = courses_stats[int(selected_course_id)]["course"]
        selected_course_grades = grades.filter(course_id=selected_course.id).order_by("-date")
        course_grades_list = list(selected_course_grades)

        def _match_any(text, keywords):
            s = (text or "").lower()
            return any(k in s for k in keywords)

        rk1_keywords = ("рк1", "рк 1", "рубеж", "рубежный 1", "рубежный контроль 1")
        rk2_keywords = ("рк2", "рк 2", "рубежный 2", "рубежный контроль 2")
        exam_keywords = ("экзамен", "final", "финал", "итог", "сессия")

        rk1_values = []
        rk2_values = []
        exam_values = []

        for g in course_grades_list:
            name = f"{getattr(g, 'assignment_name', '')} {getattr(g, 'topic', '')}".strip()
            value = float(g.value)
            if _match_any(name, exam_keywords):
                exam_values.append(value)
                continue
            if _match_any(name, rk1_keywords):
                rk1_values.append(value)
                continue
            if _match_any(name, rk2_keywords):
                rk2_values.append(value)
                continue

        selected_course_rows = [
            {
                "grade": g,
                "weekday": weekday_map.get(g.date.weekday(), "—") if g.date else "—",
            }
            for g in selected_course_grades
        ]

        if rk1_values:
            calculator_defaults["rk1"] = round(sum(rk1_values) / len(rk1_values), 1)
        if rk2_values:
            calculator_defaults["rk2"] = round(sum(rk2_values) / len(rk2_values), 1)
        if exam_values:
            calculator_defaults["exam"] = round(sum(exam_values) / len(exam_values), 1)

        calculator_defaults["has_factual_data"] = bool(
            rk1_values or rk2_values or exam_values
        )

    return render(request, 'main/grades.html', {
        'grades': grades,
        'total_grades': total_grades,
        'avg_score': avg_score,
        'courses_stats': sorted(courses_stats.values(), key=lambda x: x["course"].name),
        'selected_course_id': selected_course_id,
        'selected_course': selected_course,
        'selected_course_grades': selected_course_grades,
        'selected_course_rows': selected_course_rows,
        'all_grades_rows': all_grades_rows,
        'calculator_defaults': calculator_defaults,
    })

@login_required
def ai_assistant(request):
    """База знаний с семантическим поиском по доступным материалам."""
    try:
        user = request.user
        profile = getattr(user, 'profile', None)
        specialty = profile.specialty if profile and hasattr(profile, 'specialty') and profile.specialty else None
        is_teacher = bool(profile and profile.role == Profile.ROLE_TEACHER)
        
        # Получаем курсы пользователя по роли
        student_obj = None
        user_courses = []
        if is_teacher:
            user_courses = list(Course.objects.filter(teacher=user))
        elif profile:
            try:
                student_obj = Student.objects.get(user=user)
                enrollments = Enrollment.objects.filter(student=student_obj).select_related('course')
                user_courses = [e.course for e in enrollments if e.course]
            except Student.DoesNotExist:
                pass
            except Exception:
                pass
        
        # Поиск
        query = request.GET.get('q', '').strip()
        search_results = []
        suggested_questions = []
        focus_areas = []
        recent_materials = []

        if user_courses:
            recent_materials = list(
                _recent_materials_queryset(Lecture.objects.filter(course__in=user_courses))[:12]
            )

        if query:
            try:
                course_ids = {c.id for c in user_courses if c and hasattr(c, "id")} if user_courses else None
                all_results = semantic_search(
                    query,
                    top_k=10,
                    course_ids=course_ids if course_ids else None,
                )
                
                # Загружаем объекты лекций для результатов
                from .models import Lecture
                for result in all_results:
                    lecture_id = result.get('id')
                    if lecture_id:
                        try:
                            lecture = Lecture.objects.select_related('course').get(id=lecture_id)
                            result['lecture'] = lecture
                        except Lecture.DoesNotExist:
                            pass
                        except Exception:
                            pass
                
                # Строго ограничиваем результаты доступными курсами и только реальными материалами.
                if user_courses:
                    course_ids = {c.id for c in user_courses if c and hasattr(c, 'id')}
                    for result in all_results:
                        lecture = result.get('lecture')
                        if (
                            lecture
                            and getattr(lecture, "course_id", None) in course_ids
                            and (
                                getattr(lecture, "lecture_file", None)
                                or getattr(lecture, "content_url", "")
                                or getattr(lecture, "content_text", "")
                            )
                        ):
                            search_results.append(result)
                    search_results = search_results[:10]
                elif is_teacher:
                    search_results = []
                else:
                    # Для студента без записей на дисциплины выдача должна быть пустой.
                    search_results = []

                if not search_results:
                    # Надёжный fallback на keyword-поиск по лекциям и курсам.
                    fallback_qs = Lecture.objects.select_related("course").filter(
                        Q(title__icontains=query)
                        | Q(content_text__icontains=query)
                        | Q(course__name__icontains=query)
                    )
                    if user_courses:
                        fallback_qs = fallback_qs.filter(course__in=user_courses)
                    elif is_teacher:
                        fallback_qs = fallback_qs.none()
                    fallback_qs = fallback_qs[:10]
                    for lecture in fallback_qs:
                        snippet_source = build_lecture_snippet(lecture, query, max_len=240)
                        search_results.append(
                            {
                                "id": lecture.id,
                                "title": lecture.title,
                                "snippet": snippet_source,
                                "score": 0.0,
                                "lecture": lecture,
                            }
                        )
            except Exception as e:
                # Если поиск не работает, показываем пустые результаты
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f'AI Assistant search error: {str(e)}', exc_info=True)
                search_results = []
                messages.warning(request, 'Поиск временно недоступен. Попробуйте позже.')
        else:
            suggested_questions = []
        popular_questions = []
        
        return render(request, 'main/ai_assistant.html', {
            'query': query,
            'search_results': search_results,
            'suggested_questions': suggested_questions,
            'popular_questions': popular_questions,
            'specialty': specialty,
            'student_courses': user_courses,
            'focus_areas': focus_areas,
            'is_teacher': is_teacher,
            'recent_materials': recent_materials,
            'search_backend': search_backend_label(),
        })
    except Exception as e:
        # Общая обработка ошибок
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f'AI Assistant error: {str(e)}', exc_info=True)
        messages.error(request, f'Произошла ошибка: {str(e)}')
        return render(request, 'main/ai_assistant.html', {
            'query': request.GET.get('q', '').strip(),
            'search_results': [],
            'suggested_questions': [
                "Что такое программирование?",
                "Основы баз данных",
                "Веб-разработка для начинающих",
            ],
            'popular_questions': [],
            'specialty': None,
            'student_courses': [],
            'focus_areas': [],
            'is_teacher': False,
            'recent_materials': [],
        })


@login_required
@student_required
def ai_learning_assistant(request):
    """Академическая аналитика студента с прогнозом риска и планами вмешательства."""
    try:
        from .ai_learning_service import (
            analyze_learning_style, get_ai_recommendations,
            predict_exam_success, create_personalized_study_plan
        )
        from .models import ExamPrediction, PersonalizedStudyPlan, SmartLearningProfile
        
        user = request.user
        profile = getattr(user, 'profile', None)
        
        # Получаем курсы студента
        student_obj = None
        student_courses = []
        if profile:
            try:
                student_obj = Student.objects.get(user=user)
                enrollments = Enrollment.objects.filter(student=student_obj).select_related('course')
                student_courses = [e.course for e in enrollments if e.course]
            except Student.DoesNotExist:
                pass
            except Exception:
                pass
        
        # Анализируем стиль обучения
        learning_profile = None
        try:
            learning_profile = analyze_learning_style(user)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f'Error analyzing learning style: {str(e)}', exc_info=True)
            # Создаем базовый профиль в случае ошибки
            try:
                learning_profile, _ = SmartLearningProfile.objects.get_or_create(
                    student=user,
                    defaults={'learning_style': 'mixed'}
                )
            except Exception:
                pass
        
        # Получаем рекомендации для всех курсов
        all_recommendations = {}
        exam_predictions = {}
        study_plans = {}
        course_analytics = []
        analytics_by_course_id = {}

        def _parse_prediction_metadata(prediction):
            model_source = "unknown"
            data_completeness = None
            volatility = None
            display_risk_factors = []
            raw_factors = list(getattr(prediction, "risk_factors", []) or [])

            for factor in raw_factors:
                if not isinstance(factor, str):
                    continue
                if factor.startswith("__MODEL_SOURCE__:"):
                    model_source = factor.split(":", 1)[1].strip() or "unknown"
                    continue
                if factor.startswith("__DATA_COMPLETENESS__:"):
                    try:
                        data_completeness = float(factor.split(":", 1)[1].strip())
                    except Exception:
                        data_completeness = None
                    continue
                if factor.startswith("__VOLATILITY__:"):
                    try:
                        volatility = float(factor.split(":", 1)[1].strip())
                    except Exception:
                        volatility = None
                    continue
                display_risk_factors.append(factor)

            if model_source == "unknown":
                model_source = "trained_ml" if Path("models/grade_model.pkl").exists() else "fallback"

            prediction.model_source = model_source
            prediction.model_source_label = "Trained ML Pipeline" if model_source == "trained_ml" else "Fallback Model"
            prediction.data_completeness = data_completeness
            prediction.volatility = volatility
            prediction.display_risk_factors = display_risk_factors
            return prediction
        
        for course in student_courses:
            try:
                recommendations = get_ai_recommendations(user, course)
                all_recommendations[course.id] = recommendations
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f'Error getting recommendations for course {course.id}: {str(e)}', exc_info=True)
                all_recommendations[course.id] = []
            
            try:
                # Получаем последнее предсказание
                prediction = ExamPrediction.objects.filter(
                    student=user, course=course
                ).order_by('-created_at').first()
                if prediction:
                    prediction = _parse_prediction_metadata(prediction)
                    exam_predictions[course.id] = prediction
            except Exception:
                pass
            
            try:
                # Получаем активный план
                plan = PersonalizedStudyPlan.objects.filter(
                    student=user, course=course, is_active=True
                ).order_by('-created_at').first()
                if plan:
                    study_plans[course.id] = plan
            except Exception:
                pass

            # Явная аналитика по дисциплине: индекс, уровень риска, приоритет вмешательства.
            try:
                grades_qs = Grade.objects.filter(student=user, course=course)
                avg_grade = float(grades_qs.aggregate(avg=Avg("value"))["avg"] or 0.0)

                attendance_rate = 0.0
                if student_obj:
                    enr = Enrollment.objects.filter(student=student_obj, course=course).first()
                    if enr:
                        att_qs = Attendance.objects.filter(enrollment=enr)
                        total_att = att_qs.count()
                        present_att = att_qs.filter(present=True).count()
                        attendance_rate = (present_att * 100.0 / total_att) if total_att else 0.0

                prediction_obj = exam_predictions.get(course.id)
                success_probability = float(getattr(prediction_obj, "success_probability", 0.0) or 0.0)
                predicted_score = float(getattr(prediction_obj, "predicted_score", 0.0) or 0.0)

                # Индекс академической устойчивости 0..100
                # Опора на наблюдаемые метрики: факт оценок, посещаемость и прогноз.
                stability_index = (
                    avg_grade * 0.45
                    + attendance_rate * 0.30
                    + success_probability * 0.25
                )
                stability_index = max(0.0, min(100.0, stability_index))

                if stability_index < 45:
                    risk_level = "critical"
                    risk_label = "Критический"
                elif stability_index < 60:
                    risk_level = "high"
                    risk_label = "Высокий"
                elif stability_index < 75:
                    risk_level = "medium"
                    risk_label = "Средний"
                else:
                    risk_level = "low"
                    risk_label = "Низкий"

                intervention_priority = min(100.0, max(0.0, 100.0 - stability_index))

                risk_factors = []
                if avg_grade and avg_grade < 70:
                    risk_factors.append("Средний балл ниже 70")
                if attendance_rate and attendance_rate < 75:
                    risk_factors.append("Посещаемость ниже 75%")
                if success_probability and success_probability < 70:
                    risk_factors.append("Вероятность успеха ниже 70%")

                contribution_avg = avg_grade * 0.45
                contribution_attendance = attendance_rate * 0.30
                contribution_success = success_probability * 0.25

                prediction_meta = exam_predictions.get(course.id)
                model_source = getattr(prediction_meta, "model_source", "unknown")
                model_source_label = getattr(
                    prediction_meta,
                    "model_source_label",
                    "Trained ML Pipeline" if Path("models/grade_model.pkl").exists() else "Fallback Model",
                )
                data_completeness = getattr(prediction_meta, "data_completeness", None)
                volatility = getattr(prediction_meta, "volatility", None)

                analytics_item = {
                    "course": course,
                    "avg_grade": avg_grade,
                    "attendance_rate": attendance_rate,
                    "success_probability": success_probability,
                    "predicted_score": predicted_score,
                    "stability_index": round(stability_index, 1),
                    "intervention_priority": round(intervention_priority, 1),
                    "risk_level": risk_level,
                    "risk_label": risk_label,
                    "risk_factors": risk_factors,
                    "model_source": model_source,
                    "model_source_label": model_source_label,
                    "data_completeness": data_completeness,
                    "volatility": volatility,
                    "contribution_avg": round(contribution_avg, 1),
                    "contribution_attendance": round(contribution_attendance, 1),
                    "contribution_success": round(contribution_success, 1),
                }
                course_analytics.append(analytics_item)
                analytics_by_course_id[course.id] = analytics_item
            except Exception:
                # Не роняем страницу, если по отдельному курсу данных недостаточно.
                continue

        # Сводный фактор посещаемости на уровне остальных факторов профиля
        attendance_profile = None
        try:
            if student_obj:
                all_att = Attendance.objects.filter(enrollment__student=student_obj)
                total_att = all_att.count()
                present_att = all_att.filter(present=True).count()
                attendance_profile = (present_att * 100 / total_att) if total_att else None
        except Exception:
            attendance_profile = None

        if course_analytics:
            analytics_sorted = sorted(
                course_analytics,
                key=lambda item: item["intervention_priority"],
                reverse=True,
            )
            overall_stability = round(
                sum(item["stability_index"] for item in course_analytics) / len(course_analytics), 1
            )
            courses_at_risk = sum(
                1 for item in course_analytics if item["risk_level"] in ("critical", "high")
            )
        else:
            analytics_sorted = []
            overall_stability = 0.0
            courses_at_risk = 0

        return render(request, 'main/ai_learning_assistant.html', {
            'learning_profile': learning_profile,
            'student_courses': student_courses,
            'recommendations': all_recommendations,
            'exam_predictions': exam_predictions,
            'study_plans': study_plans,
            'attendance_profile': attendance_profile,
            'course_analytics': analytics_sorted,
            'analytics_by_course_id': analytics_by_course_id,
            'overall_stability': overall_stability,
            'courses_at_risk': courses_at_risk,
        })
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f'AI Learning Assistant error: {str(e)}', exc_info=True)
        messages.error(request, f'Произошла ошибка при загрузке умного ассистента. Попробуйте позже.')
        return render(request, 'main/ai_learning_assistant.html', {
            'learning_profile': None,
            'student_courses': [],
            'recommendations': {},
            'exam_predictions': {},
            'study_plans': {},
            'attendance_profile': None,
            'course_analytics': [],
            'analytics_by_course_id': {},
            'overall_stability': 0.0,
            'courses_at_risk': 0,
        })


@login_required
@student_required
def predict_exam_view(request, course_id):
    """Предсказание успеха на экзамене"""
    from .ai_learning_service import predict_exam_success
    from .models import Course
    
    course = get_object_or_404(Course, id=course_id)
    if not Enrollment.objects.filter(student__user=request.user, course=course).exists():
        messages.error(request, 'Недостаточно прав для прогноза по этому курсу.')
        return redirect('ai_learning_assistant')
    
    if request.method == 'POST':
        exam_date_str = request.POST.get('exam_date')
        exam_date = None
        if exam_date_str:
            try:
                from datetime import datetime
                exam_date = datetime.strptime(exam_date_str, '%Y-%m-%d')
                exam_date = timezone.make_aware(exam_date)
            except Exception:
                pass
        
        try:
            prediction = predict_exam_success(request.user, course, exam_date)
            messages.success(request, 'Предсказание успешно создано!')
            return redirect('ai_learning_assistant')
        except Exception as e:
            messages.error(request, f'Ошибка: {str(e)}')
    
    return redirect('ai_learning_assistant')


@login_required
@student_required
def create_study_plan_view(request, course_id):
    """Создание персонализированного плана обучения"""
    from .ai_learning_service import create_personalized_study_plan
    from .models import Course
    
    course = get_object_or_404(Course, id=course_id)
    if not Enrollment.objects.filter(student__user=request.user, course=course).exists():
        messages.error(request, 'Недостаточно прав для создания плана по этому курсу.')
        return redirect('ai_learning_assistant')
    
    if request.method == 'POST':
        target_date_str = request.POST.get('target_date')
        if not target_date_str:
            messages.error(request, 'Укажите целевую дату')
            return redirect('ai_learning_assistant')
        
        try:
            from datetime import datetime
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d')
            target_date = timezone.make_aware(target_date)
            
            if target_date <= timezone.now():
                messages.error(request, 'Целевая дата должна быть в будущем')
                return redirect('ai_learning_assistant')
            
            plan = create_personalized_study_plan(request.user, course, target_date)
            messages.success(request, f'Персонализированный план создан! Всего часов: {plan.total_hours}')
            return redirect('ai_learning_assistant')
        except Exception as e:
            messages.error(request, f'Ошибка: {str(e)}')
    
    return redirect('ai_learning_assistant')


# ===== Публичные академические страницы =====

@login_required
@teacher_required
def groups_list(request):
    """Список всех групп"""
    groups = Group.objects.filter(
        student_group__enrollments__course__teacher=request.user
    ).distinct().order_by('-year', 'name')
    # Добавляем количество студентов в каждой группе
    for group in groups:
        group.student_count = Profile.objects.filter(group=group, role=Profile.ROLE_STUDENT).count()
        group.courses_count = (
            Course.objects.filter(enrollments__student__group=group).distinct().count()
        )
    return render(request, 'main/groups_list.html', {'groups': groups})

@login_required
@teacher_required
def group_schedule(request, group_id: int):
    group = get_object_or_404(Group, id=group_id)
    # Получаем профили студентов этой группы
    group_profiles = Profile.objects.filter(group=group, role=Profile.ROLE_STUDENT)
    schedule = (
        ScheduleEntry.objects.filter(
            groups__in=group_profiles,
            course__teacher=request.user,
        )
        .select_related("course")
        .distinct()
        .order_by("weekday", "start_time")
    )
    return render(
        request,
        "main/group_schedule.html",
        {
            "group": group,
            "schedule": schedule,
        },
    )


@login_required
@teacher_required
def student_public_profile(request, pk: int):
    student = get_object_or_404(Student, id=pk)
    has_relationship = Enrollment.objects.filter(
        student=student,
        course__teacher=request.user,
    ).exists()
    if not has_relationship:
        return _deny_and_redirect(
            request,
            "Вы можете просматривать только студентов своих курсов.",
            "teacher_dashboard",
        )

    enrollments = (
        Enrollment.objects.filter(student=student, course__teacher=request.user)
        .select_related("course")
        .order_by("course__name")
    )
    course_stats = []
    for enr in enrollments:
        grades_qs = Grade.objects.filter(enrollment=enr)
        attendance_qs = Attendance.objects.filter(enrollment=enr)
        attendance_rate = None
        if attendance_qs.exists():
            total = attendance_qs.count()
            present = attendance_qs.filter(present=True).count()
            attendance_rate = present * 100 / total if total else None

        avg_grade = grades_qs.aggregate(avg=Avg("value"))["avg"]
        course_stats.append(
            {
                "course": enr.course,
                "attendance_rate": attendance_rate,
                "avg_grade": avg_grade,
            }
        )

    return render(
        request,
        "main/student_public_profile.html",
        {
            "student_obj": student,
            "course_stats": course_stats,
        },
    )


@login_required
def course_lectures(request, pk: int):
    course = get_object_or_404(Course, pk=pk)
    if not _user_can_access_course(request.user, course):
        return _deny_and_redirect(request, "У вас нет доступа к лекциям этого курса.")

    lectures = Lecture.objects.filter(course=course).order_by("created_at")
    q = request.GET.get("q", "").strip()
    search_results = None
    if q:
        search_results = semantic_search(q, top_k=10, course_ids=[course.id])
    return render(
        request,
        "main/course_lectures.html",
        {
            "course": course,
            "lectures": lectures,
            "q": q,
            "search_results": search_results,
        },
    )


@login_required
def lecture_detail(request, pk: int):
    lecture = get_object_or_404(Lecture, pk=pk)
    if not _user_can_access_course(request.user, lecture.course):
        return _deny_and_redirect(request, "У вас нет доступа к этому материалу.")

    related = [
        r
        for r in semantic_search(lecture.title, top_k=8, course_ids=[lecture.course_id])
        if r.get("id") != lecture.id
    ][:5]
    return render(
        request,
        "main/lecture_detail.html",
        {
            "lecture": lecture,
            "related": related,
        },
    )


@login_required
def download_lecture_file(request, pk: int):
    lecture = get_object_or_404(Lecture.objects.select_related("course"), pk=pk)

    user_profile = getattr(request.user, "profile", None)
    if request.user.is_staff and not user_profile:
        has_access = True
    elif user_profile and user_profile.role == Profile.ROLE_TEACHER:
        has_access = lecture.course.teacher_id == request.user.id
    else:
        has_access = Enrollment.objects.filter(
            student__user=request.user,
            course=lecture.course,
        ).exists()

    if not has_access:
        return _deny_and_redirect(request, "У вас нет доступа к этому материалу.")

    if not _supports_lecture_file() or not getattr(lecture, "lecture_file", None):
        messages.error(request, "Файл лекции не найден.")
        return redirect("course_detail", pk=lecture.course_id)

    file_path = Path(lecture.lecture_file.path)
    if not file_path.exists():
        raise Http404("Файл не найден на сервере.")

    if file_path.stat().st_size == 0:
        messages.error(request, "Файл повреждён или пустой. Попросите преподавателя загрузить его заново.")
        return redirect("course_detail", pk=lecture.course_id)

    response = FileResponse(
        open(file_path, "rb"),
        as_attachment=True,
        filename=file_path.name,
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@login_required
@staff_required
def demo_page(request):
    # Несколько случайных студентов и курсов для демонстрации
    students = Student.objects.all()[:10]
    courses = Course.objects.all()[:10]
    q = request.GET.get("q", "").strip()
    results = None
    if q:
        results = semantic_search(q, top_k=5)
    return render(
        request,
        "main/demo.html",
        {
            "students": students,
            "courses": courses,
            "q": q,
            "results": results,
        },
    )

# ===== Преподавательские страницы =====
@login_required
@teacher_required
def teacher_dashboard(request):
    user = request.user
    courses = Course.objects.filter(teacher=user).select_related('subject')
    lecture_form = LectureCreateForm(teacher=user)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_resource":
            lecture_form = LectureCreateForm(request.POST, request.FILES, teacher=user)
            if lecture_form.is_valid():
                lecture_form.save()
                messages.success(request, "Лекция/ресурс добавлены.")
                return redirect("teacher_dashboard")

    total_students = User.objects.filter(
        profile__role=Profile.ROLE_STUDENT,
        grades__course__teacher=user
    ).distinct().count()
    
    total_groups = Group.objects.filter(
        student_group__enrollments__course__teacher=user
    ).distinct().count()
    total_materials = Lecture.objects.filter(course__teacher=user).count()
    
    recent_grades = Grade.objects.filter(course__teacher=user).select_related('student', 'course').order_by('-date')[:10]
    advisee_enrollments = (
        Enrollment.objects.filter(course__teacher=user, student__user__isnull=False)
        .select_related("student__user", "course")
        .order_by("student__last_name", "student__first_name")
    )
    advisee_map = {}
    for enrollment in advisee_enrollments:
        student_user = enrollment.student.user
        if not student_user:
            continue
        row = advisee_map.setdefault(
            student_user.id,
            {
                "student_user": student_user,
                "courses": [],
            },
        )
        row["courses"].append(enrollment.course.name)

    advisee_students = []
    for row in advisee_map.values():
        report = build_student_performance_report(row["student_user"], teacher=user)
        student_obj = Student.objects.filter(user=row["student_user"]).first()
        advisee_students.append(
            {
                "student_user": row["student_user"],
                "student_obj": student_obj,
                "courses": ", ".join(sorted(set(row["courses"]))),
                "risk_level": report["overall_risk_level"],
                "risk_score": report["overall_risk_score"],
                "overall_avg": report["overall_avg"],
            }
        )
    advisee_students.sort(key=lambda item: item["risk_score"], reverse=True)
    student_query = request.GET.get("student_q", "").strip().lower()
    risk_filter = request.GET.get("risk", "").strip().lower()
    if student_query:
        advisee_students = [
            row for row in advisee_students
            if student_query in (row["student_user"].get_full_name() or row["student_user"].username).lower()
            or student_query in row["courses"].lower()
        ]
    if risk_filter in {"critical", "high", "medium", "low"}:
        advisee_students = [row for row in advisee_students if row["risk_level"] == risk_filter]

    hierarchy = []
    groups_qs = Group.objects.filter(
        student_group__enrollments__course__teacher=user
    ).distinct().order_by("course_year", "name")
    for group in groups_qs:
        group_students = [row for row in advisee_students if row.get("student_obj") and row["student_obj"].group_id == group.id]
        disciplines = Course.objects.filter(
            teacher=user, enrollments__student__group=group
        ).distinct().order_by("name")
        hierarchy.append(
            {
                "course_year": group.course_year,
                "group": group,
                "disciplines": disciplines,
                "students": group_students,
            }
        )

    paginator = Paginator(advisee_students, 20)
    advisee_page = paginator.get_page(request.GET.get("page"))
    published_lectures = list(
        _recent_materials_queryset(Lecture.objects.filter(course__teacher=user))[:12]
    )
    
    return render(request, 'main/teacher_dashboard.html', {
        'courses': courses,
        'total_disciplines': courses.count(),
        'total_groups': total_groups,
        'total_students': total_students,
        'total_materials': total_materials,
        'recent_grades': recent_grades,
        'lecture_form': lecture_form,
        'advisee_students': advisee_page.object_list,
        'advisee_page': advisee_page,
        'student_q': request.GET.get("student_q", "").strip(),
        'risk_filter': risk_filter,
        'published_lectures': published_lectures,
        'hierarchy': hierarchy,
    })

@login_required
@teacher_required
def teacher_courses(request):
    courses = Course.objects.filter(teacher=request.user).prefetch_related("lectures")
    course_cards = []
    for course in courses:
        group_years = Group.objects.filter(
            student_group__enrollments__course=course
        ).values_list("course_year", flat=True).distinct().order_by("course_year")
        materials = [
            lecture
            for lecture in course.lectures.all()
            if getattr(lecture, "lecture_file", None) or lecture.content_url or lecture.content_text
        ]
        latest_material = materials[0] if materials else None
        course_cards.append(
            {
                "course": course,
                "materials_count": len(materials),
                "latest_material": latest_material,
                "course_years": ", ".join(str(y) for y in group_years) if group_years else "—",
            }
        )
    return render(request, 'main/teacher_courses.html', {'courses': courses, 'course_cards': course_cards})


@login_required
@teacher_required
def teacher_discipline_detail(request, pk: int):
    course = get_object_or_404(Course, pk=pk, teacher=request.user)
    lecture_form = LectureCreateForm(
        request.POST or None,
        request.FILES or None,
        teacher=request.user,
        initial={"course": course},
    )
    if request.method == "POST" and lecture_form.is_valid():
        lecture = lecture_form.save(commit=False)
        lecture.course = course
        lecture.save()
        messages.success(request, "Материал по дисциплине успешно опубликован.")
        return redirect("teacher_discipline_detail", pk=course.id)

    lectures = list(course.lectures.all().order_by("-created_at"))
    weekly_materials = defaultdict(list)
    for lecture in lectures:
        week_key = lecture.created_at.isocalendar().week if lecture.created_at else 0
        weekly_materials[week_key].append(lecture)

    grouped_weeks = [
        {"week": week, "lectures": items}
        for week, items in sorted(weekly_materials.items(), key=lambda x: x[0], reverse=True)
    ]
    generated_quizzes = LectureQuiz.objects.filter(course=course).select_related("lecture", "assignment")
    quiz_stats = []
    for quiz in generated_quizzes:
        attempts = list(quiz.attempts.all())
        attempts_count = len(attempts)
        avg_score = round(sum(float(a.score) for a in attempts) / attempts_count, 2) if attempts_count else 0.0
        question_stats = []
        for q in quiz.questions.all():
            total = 0
            correct = 0
            for attempt in attempts:
                selected = (attempt.answers or {}).get(str(q.id))
                if selected:
                    total += 1
                    if selected == q.correct_option:
                        correct += 1
            success_rate = (correct * 100.0 / total) if total else 0.0
            question_stats.append({"question": q, "success_rate": round(success_rate, 1), "answered": total})
        hardest_questions = sorted(question_stats, key=lambda x: x["success_rate"])[:3]
        quiz_stats.append(
            {
                "quiz": quiz,
                "attempts_count": attempts_count,
                "avg_score": avg_score,
                "hardest_questions": hardest_questions,
            }
        )
    return render(
        request,
        "main/teacher_discipline_detail.html",
        {
            "course": course,
            "lecture_form": lecture_form,
            "grouped_weeks": grouped_weeks,
            "generated_quizzes": generated_quizzes,
            "quiz_stats": quiz_stats,
        },
    )


@login_required
@teacher_required
def generate_lecture_quiz_view(request, pk: int):
    course = get_object_or_404(Course, pk=pk, teacher=request.user)
    if request.method != "POST":
        return redirect("teacher_discipline_detail", pk=course.id)

    lecture_id = (request.POST.get("lecture_id") or "").strip()
    quiz_title = (request.POST.get("quiz_title") or "").strip() or f"Тест по дисциплине {course.name}"
    source_text = (request.POST.get("source_text") or "").strip()
    try:
        question_count = int(request.POST.get("question_count") or 5)
    except Exception:
        question_count = 5
    try:
        max_attempts = int(request.POST.get("max_attempts") or 1)
    except Exception:
        max_attempts = 1
    try:
        time_limit_minutes = int(request.POST.get("time_limit_minutes") or 20)
    except Exception:
        time_limit_minutes = 20
    question_count = max(3, min(12, question_count))
    max_attempts = max(1, min(10, max_attempts))
    time_limit_minutes = max(5, min(180, time_limit_minutes))

    lecture = None
    if lecture_id.isdigit():
        lecture = Lecture.objects.filter(id=int(lecture_id), course=course).first()
        if lecture and not source_text:
            source_text = (lecture.content_text or "").strip()

    if not source_text:
        messages.error(request, "Добавьте текст для генерации теста или выберите лекцию с текстовым содержанием.")
        return redirect("teacher_discipline_detail", pk=course.id)

    generated_questions = _build_quiz_questions_from_text(source_text, question_count)
    if len(generated_questions) < 3:
        messages.error(
            request,
            "Недостаточно содержательного текста для генерации теста. Добавьте больше предложений в материал.",
        )
        return redirect("teacher_discipline_detail", pk=course.id)

    assignment = Assignment.objects.create(
        course=course,
        title=quiz_title,
        description="Автоматически сгенерированный тест по материалу лекции.",
        due_date=timezone.now() + timedelta(days=7),
        max_score=100,
        topic=lecture.title if lecture else "Тест по материалу",
        assignment_type="quiz",
    )
    quiz = LectureQuiz.objects.create(
        course=course,
        lecture=lecture,
        assignment=assignment,
        title=quiz_title,
        source_text=source_text[:12000],
        generated_by=request.user,
        question_count=len(generated_questions),
        max_attempts=max_attempts,
        time_limit_minutes=time_limit_minutes,
        is_active=True,
    )
    for q in generated_questions:
        LectureQuizQuestion.objects.create(
            quiz=quiz,
            question_text=q["question_text"],
            option_a=q["options"][0],
            option_b=q["options"][1],
            option_c=q["options"][2],
            option_d=q["options"][3],
            correct_option=q["correct_letter"],
            order=q["order"],
        )

    messages.success(
        request,
        f"Тест «{quiz.title}» создан: {len(generated_questions)} вопросов. Студенты уже могут проходить его в курсе.",
    )
    return redirect("teacher_discipline_detail", pk=course.id)


@login_required
@student_required
def take_quiz_view(request, quiz_id: int):
    quiz = get_object_or_404(
        LectureQuiz.objects.select_related("course", "assignment"),
        id=quiz_id,
        is_active=True,
    )
    has_access = Enrollment.objects.filter(student__user=request.user, course=quiz.course).exists()
    if not has_access:
        messages.error(request, "Тест доступен только студентам, записанным на дисциплину.")
        return redirect("dashboard")

    attempts_count = LectureQuizAttempt.objects.filter(quiz=quiz, student=request.user).count()
    if attempts_count >= quiz.max_attempts:
        messages.error(request, "Лимит попыток исчерпан для этого теста.")
        return redirect("course_detail", pk=quiz.course.id)

    questions = list(quiz.questions.all().order_by("order", "id"))
    rnd = random.Random(f"{quiz.id}:{request.user.id}")
    rnd.shuffle(questions)

    options_by_question = {}
    for q in questions:
        opts = [
            ("A", q.option_a),
            ("B", q.option_b),
            ("C", q.option_c),
            ("D", q.option_d),
        ]
        rnd.shuffle(opts)
        options_by_question[q.id] = opts

    start_key = f"quiz_start_{quiz.id}_{request.user.id}"
    if request.method == "GET":
        request.session[start_key] = timezone.now().isoformat()
        request.session.modified = True

    if request.method == "POST":
        started_at_raw = request.session.get(start_key)
        if started_at_raw:
            try:
                started_at = datetime.fromisoformat(started_at_raw)
                if timezone.is_naive(started_at):
                    started_at = timezone.make_aware(started_at)
                elapsed_minutes = (timezone.now() - started_at).total_seconds() / 60.0
                if elapsed_minutes > quiz.time_limit_minutes:
                    messages.error(request, "Время на выполнение теста истекло. Попытка не засчитана.")
                    return redirect("course_detail", pk=quiz.course.id)
            except Exception:
                pass

        answers = {}
        correct = 0
        for question in questions:
            key = f"q_{question.id}"
            selected = (request.POST.get(key) or "").strip().upper()
            if selected in {"A", "B", "C", "D"}:
                answers[str(question.id)] = selected
                if selected == question.correct_option:
                    correct += 1
        total = len(questions)
        score = round((correct * 100.0 / total), 2) if total else 0.0

        LectureQuizAttempt.objects.create(
            quiz=quiz,
            student=request.user,
            score=score,
            total_questions=total,
            answers=answers,
        )

        enrollment = Enrollment.objects.filter(student__user=request.user, course=quiz.course).first()
        existing_grade = Grade.objects.filter(
            student=request.user,
            course=quiz.course,
            assignment=quiz.assignment,
        ).first()
        if existing_grade:
            existing_grade.value = score
            existing_grade.topic = f"Тест: {quiz.title}"
            existing_grade.comment = f"Автооценка: {correct}/{total} правильных ответов."
            existing_grade.date = timezone.now()
            existing_grade.assignment_name = quiz.assignment.title
            existing_grade.enrollment = enrollment
            existing_grade.save()
        else:
            Grade.objects.create(
                student=request.user,
                course=quiz.course,
                enrollment=enrollment,
                assignment=quiz.assignment,
                assignment_name=quiz.assignment.title,
                value=score,
                topic=f"Тест: {quiz.title}",
                comment=f"Автооценка: {correct}/{total} правильных ответов.",
                date=timezone.now(),
            )

        messages.success(request, f"Тест завершён. Результат: {score}%. Оценка сохранена в журнал.")
        return redirect("course_detail", pk=quiz.course.id)

    return render(
        request,
        "main/take_quiz.html",
        {
            "quiz": quiz,
            "questions": questions,
            "options_by_question": options_by_question,
            "attempts_left": max(0, quiz.max_attempts - attempts_count),
        },
    )

@login_required
@teacher_required
def teacher_grades(request):
    teacher = request.user
    selected_year = request.GET.get("course_year", "").strip()
    selected_group_id = request.GET.get("group_id", "").strip()
    selected_student_id = request.GET.get("student_id", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    groups_qs = Group.objects.filter(
        student_group__enrollments__course__teacher=teacher
    ).distinct().order_by("course_year", "name")
    if selected_year.isdigit():
        groups_qs = groups_qs.filter(course_year=int(selected_year))

    students_qs = Student.objects.filter(
        enrollments__course__teacher=teacher
    ).distinct().order_by("last_name", "first_name")
    if selected_group_id.isdigit():
        students_qs = students_qs.filter(group_id=int(selected_group_id))

    courses_qs = Course.objects.filter(teacher=teacher).order_by("name")
    if selected_group_id.isdigit():
        courses_qs = courses_qs.filter(enrollments__student__group_id=int(selected_group_id)).distinct()

    grade_entry_form = TeacherGradeEntryForm(
        request.POST or None,
        teacher=teacher,
        initial={"course_year": selected_year, "group": selected_group_id},
    )
    if request.method == "POST" and grade_entry_form.is_valid():
        student = grade_entry_form.cleaned_data["student"]
        course = grade_entry_form.cleaned_data["course"]
        grade_date = grade_entry_form.cleaned_data["date"]
        enrollment = Enrollment.objects.filter(student=student, course=course).first()
        Grade.objects.create(
            student=student.user,
            course=course,
            enrollment=enrollment,
            value=grade_entry_form.cleaned_data["value"],
            topic=grade_entry_form.cleaned_data["topic"] or "",
            comment=grade_entry_form.cleaned_data["comment"] or "",
            date=timezone.make_aware(datetime.combine(grade_date, time.min)),
            assignment_name=f"Оценка от {grade_date.strftime('%d.%m.%Y')}",
        )
        messages.success(request, "Оценка сохранена.")
        return redirect(
            f"{reverse('teacher_grades')}?course_year={selected_year}&group_id={selected_group_id}&student_id={selected_student_id}&date_from={date_from}&date_to={date_to}"
        )

    selected_student = None
    selected_student_user = None
    student_grades = Grade.objects.none()
    student_report = None

    if selected_student_id.isdigit():
        selected_student = students_qs.filter(id=int(selected_student_id)).first()
        if selected_student and selected_student.user:
            selected_student_user = selected_student.user
            student_grades = Grade.objects.filter(
                student=selected_student_user,
                course__teacher=teacher,
            ).select_related("course").order_by("-date")
            if date_from:
                student_grades = student_grades.filter(date__date__gte=date_from)
            if date_to:
                student_grades = student_grades.filter(date__date__lte=date_to)
            student_report = build_student_performance_report(selected_student_user, teacher=teacher)

    return render(
        request,
        "main/teacher_grades.html",
        {
            "groups": groups_qs,
            "students": students_qs,
            "courses": courses_qs,
            "selected_year": selected_year,
            "selected_group_id": selected_group_id,
            "selected_student_id": selected_student_id,
            "date_from": date_from,
            "date_to": date_to,
            "selected_student": selected_student,
            "student_grades": student_grades[:200],
            "student_report": student_report,
            "grade_entry_form": grade_entry_form,
        },
    )

@login_required
@teacher_required
def teacher_schedule(request):
    schedule = ScheduleEntry.objects.filter(
        course__teacher=request.user
    ).select_related('course').prefetch_related('groups').order_by('weekday', 'start_time')
    days = [
        (0, "Понедельник"),
        (1, "Вторник"),
        (2, "Среда"),
        (3, "Четверг"),
        (4, "Пятница"),
        (5, "Суббота"),
    ]
    time_slots = sorted({(str(item.start_time), str(item.end_time)) for item in schedule})
    grid = []
    for start, end in time_slots:
        row = {"time": f"{start} - {end}", "cells": []}
        for day_idx, _ in days:
            items = [
                e for e in schedule
                if e.weekday == day_idx and str(e.start_time) == start and str(e.end_time) == end
            ]
            row["cells"].append(items)
        grid.append(row)

    return render(
        request,
        'main/teacher_schedule.html',
        {'schedule': schedule, 'days': days, 'grid': grid},
    )

# ===== ML/AI функции для оценивания =====
def analyze_student_performance(student, course):
    """Анализирует успеваемость студента и предсказывает проблемные темы"""
    grades = Grade.objects.filter(student=student, course=course)
    
    if not grades.exists():
        return None
    
    # Простой алгоритм анализа
    topics = {}
    for grade in grades:
        if grade.topic:
            if grade.topic not in topics:
                topics[grade.topic] = []
            topics[grade.topic].append(float(grade.value))
    
    problem_areas = []
    recommendations = []
    
    for topic, scores in topics.items():
        avg_score = sum(scores) / len(scores)
        if avg_score < 60:
            problem_areas.append({
                'topic': topic,
                'avg_score': avg_score,
                'severity': 'high' if avg_score < 50 else 'medium'
            })
            recommendations.append(f"Рекомендуется дополнительная работа по теме '{topic}'")
    
    return {
        'problem_areas': problem_areas,
        'recommendations': recommendations,
        'overall_avg': grades.aggregate(avg=Avg('value'))['avg'] or 0
    }


@login_required
@teacher_required
def teacher_student_analysis(request, student_id):
    student_user = get_object_or_404(User, id=student_id)
    student_obj = Student.objects.filter(user=student_user).first()
    if not student_obj:
        messages.error(request, "У выбранного пользователя отсутствует профиль студента.")
        return redirect("teacher_dashboard")

    has_relationship = Enrollment.objects.filter(
        student=student_obj,
        course__teacher=request.user,
    ).exists()
    if not has_relationship:
        messages.error(request, "Вы можете анализировать только студентов своих курсов.")
        return redirect("teacher_dashboard")

    report = build_student_performance_report(student_user, teacher=request.user)
    return render(
        request,
        "main/teacher_student_analysis.html",
        {
            "student": student_user,
            "report": report,
        },
    )


@login_required
@teacher_required
def ai_analysis_view(request, student_id, course_id):
    """Совместимость старого URL: редирект на расширенный анализ студента."""
    student = get_object_or_404(User, id=student_id)
    course = get_object_or_404(Course, id=course_id, teacher=request.user)
    messages.info(request, f"Открыт расширенный анализ по курсу: {course.name}")
    return redirect("teacher_student_analysis", student_id=student.id)


# ===== API: ML прогноз и поиск =====


@login_required
@staff_required
def api_predict_grade(request):
    if _rate_limit_exceeded(request, "api_predict_grade", limit=40, window_seconds=60):
        return JsonResponse({"detail": "Слишком много запросов. Повторите позже."}, status=429)

    if request.method != "POST":
        return JsonResponse({"detail": "Только POST"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "Некорректный JSON"}, status=400)

    student_id = payload.get("student_id")
    course_id = payload.get("course_id")
    if not student_id or not course_id:
        return JsonResponse({"detail": "Нужно указать student_id и course_id"}, status=400)

    try:
        enrollment = Enrollment.objects.get(student_id=student_id, course_id=course_id)
    except Enrollment.DoesNotExist:
        return JsonResponse({"detail": "Запись студента на курс не найдена"}, status=404)

    # Загружаем модель
    from pathlib import Path

    model_path = Path("models/grade_model.pkl")
    if not model_path.exists():
        return JsonResponse({"detail": "Модель ещё не обучена. Запустите train_grade_model."}, status=503)

    import joblib  # type: ignore

    bundle = joblib.load(model_path)
    model = bundle["model"]
    scaler = bundle["scaler"]
    feature_names = bundle["feature_names"]

    # Формируем признаки по той же логике, что и в train_grade_model
    grades_qs = Grade.objects.filter(enrollment=enrollment)
    from django.db.models import Avg

    att_qs = Attendance.objects.filter(enrollment=enrollment)
    attendance_rate = 1.0
    if att_qs.exists():
        total = att_qs.count()
        present = att_qs.filter(present=True).count()
        attendance_rate = present / total if total else 1.0

    hw_avg = grades_qs.filter(assignment_name__icontains="Домашнее").aggregate(
        avg=Avg("value")
    )["avg"]
    if hw_avg is None:
        hw_avg = grades_qs.exclude(assignment_name__icontains="Финал").aggregate(
            avg=Avg("value")
        )["avg"] or 0

    midterm = grades_qs.filter(assignment_name__icontains="Midterm").order_by(
        "-date"
    ).first()
    midterm_score = float(midterm.value) if midterm else float(hw_avg)

    previous_grades = Grade.objects.filter(
        student=enrollment.student.user if enrollment.student.user else None
    ).exclude(course=enrollment.course)
    if previous_grades.exists():
        previous_gpa = float(previous_grades.aggregate(avg=Avg("value"))["avg"] or 0)
    else:
        previous_gpa = float(hw_avg)

    features = [
        float(attendance_rate * 100.0),
        float(hw_avg),
        float(midterm_score),
        float(previous_gpa),
    ]

    x = scaler.transform([features])
    pred = float(model.predict(x)[0])

    # Простое объяснение: вклад признаков (для линейной модели)
    contributions = {}
    coef = getattr(model, "coef_", None)
    if coef is not None:
        for name, c, val in zip(feature_names, coef, features):
            contributions[name] = float(c * val)
        confidence = 0.8
    else:
        # Для деревьев используем feature_importances_
        importances = getattr(model, "feature_importances_", None)
        if importances is not None:
            for name, imp in zip(feature_names, importances):
                contributions[name] = float(imp)
            confidence = float(max(importances) if len(importances) else 0.5)
        else:
            confidence = 0.5

    return JsonResponse(
        {
            "predicted_final_grade": pred,
            "model_confidence": confidence,
            "feature_contributions": contributions,
        }
    )


def api_search_resources(request):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Требуется авторизация"}, status=401)

    if _rate_limit_exceeded(request, "api_search_resources", limit=60, window_seconds=60):
        return JsonResponse({"detail": "Слишком много запросов. Повторите позже."}, status=429)

    if request.method != "POST":
        return JsonResponse({"detail": "Только POST"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "Некорректный JSON"}, status=400)

    q = (payload.get("q") or "").strip()
    top_k = int(payload.get("top_k") or 5)
    top_k = max(1, min(top_k, 20))

    results = semantic_search(q, top_k=top_k)
    return JsonResponse({"results": results})


@login_required
@staff_required
def api_retrain_embeddings(request):
    if _rate_limit_exceeded(request, "api_retrain_embeddings", limit=5, window_seconds=300):
        return JsonResponse({"detail": "Слишком много запросов. Повторите позже."}, status=429)

    if request.method != "POST":
        return JsonResponse({"detail": "Только POST"}, status=405)
    from django.core.management import call_command

    call_command("index_lectures")
    return JsonResponse({"detail": "Индексация лекций запущена."})
