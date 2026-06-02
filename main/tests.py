from pathlib import Path
import json

from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse

from django.contrib.auth.models import User

from .models import Student, Group, Course, Lecture, Profile, Enrollment


class SeedDemoTests(TestCase):
    def test_seed_demo_creates_data(self):
        call_command("seed_demo", students=10, groups=3, courses=5, seed=1)
        self.assertGreater(Student.objects.count(), 0)
        self.assertGreater(Group.objects.count(), 0)
        self.assertGreater(Course.objects.count(), 0)
        self.assertGreater(Lecture.objects.count(), 0)

        # Идемпотентность: повторный запуск не должен падать
        call_command("seed_demo", students=10, groups=3, courses=5, seed=1)


class TrainModelTests(TestCase):
    def setUp(self):
        call_command("seed_demo", students=30, groups=5, courses=5, seed=2)

    def test_train_grade_model_outputs_files(self):
        models_dir = Path("models")
        model_path = models_dir / "grade_model.pkl"
        metrics_path = models_dir / "metrics.json"

        call_command("train_grade_model", save_path=str(model_path))

        self.assertTrue(model_path.exists())
        self.assertTrue(metrics_path.exists())

        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        self.assertIn("rmse", metrics)
        self.assertIn("r2", metrics)


class SearchServiceTests(TestCase):
    def setUp(self):
        call_command("seed_demo", students=10, groups=2, courses=3, seed=9)

    def test_semantic_search_without_vectors_returns_results(self):
        from main.search_service import semantic_search

        Lecture.objects.update(vector_embedding=None)
        results = semantic_search("программирование", top_k=5)
        self.assertTrue(len(results) > 0)
        self.assertIn("title", results[0])
        self.assertIn("snippet", results[0])

    def test_hybrid_search_finds_body_text(self):
        from main.search_service import hybrid_search_for_lectures

        course = Course.objects.first()
        Lecture.objects.create(
            course=course,
            title="Алгоритмы",
            content_text="Сортировка пузырьком и бинарный поиск в массиве.",
        )
        results = hybrid_search_for_lectures(
            "пузырьков сортировка",
            Lecture.objects.filter(course=course),
            limit=5,
        )
        self.assertTrue(results)
        joined = " ".join(
            (r.get("title") or "") + " " + (r.get("snippet") or "") for r in results
        ).lower()
        self.assertIn("пузырьк", joined)


class ApiTests(TestCase):
    def setUp(self):
        call_command("seed_demo", students=20, groups=3, courses=3, seed=3)
        # Обучаем модель для предсказания
        models_dir = Path("models")
        model_path = models_dir / "grade_model.pkl"
        call_command("train_grade_model", save_path=str(model_path))
        # Индексируем лекции (может упасть в BM25 fallback, это нормально)
        call_command("index_lectures")

        self.staff = User.objects.create_user(
            username="admin", password="admin123", is_staff=True
        )

    def test_predict_grade_api(self):
        from .models import Enrollment

        enrollment = Enrollment.objects.first()
        client = Client()
        client.login(username="admin", password="admin123")

        url = reverse("api_predict_grade")
        resp = client.post(
            url,
            data=json.dumps(
                {
                    "student_id": enrollment.student_id,
                    "course_id": enrollment.course_id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("predicted_final_grade", data)

    def test_search_resources_api(self):
        client = Client()
        client.login(username="admin", password="admin123")
        url = reverse("api_search_resources")
        resp = client.post(
            url,
            data=json.dumps({"q": "программирование", "top_k": 3}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("results", data)


class KnowledgeBaseViewTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="kb_teacher",
            password="pass12345",
            is_staff=True,
        )
        Profile.objects.update_or_create(
            user=self.teacher,
            defaults={"role": Profile.ROLE_TEACHER},
        )
        self.course = Course.objects.create(
            name="KB Course",
            code="KB101",
            teacher=self.teacher,
        )
        Lecture.objects.create(
            course=self.course,
            title="Тест лекция",
            content_text="Ответ да или нет на экзамене по алгоритмам.",
        )

    def test_ai_assistant_search_does_not_crash(self):
        client = Client()
        client.login(username="kb_teacher", password="pass12345")
        resp = client.get("/ai-assistant/?q=статья")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8", errors="replace")
        self.assertNotIn("UnboundLocalError", body)
        self.assertNotIn("cannot access local variable", body)
        self.assertIn("2026-06-03-v4", body)

    def test_deploy_version_endpoint(self):
        resp = self.client.get("/__deploy_version__/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"2026-06-03-v4", resp.content)

    def test_kb_finds_content_text(self):
        from main.kb import search_lectures

        results = search_lectures(self.teacher, "алгоритм", limit=5)
        self.assertTrue(results)
        self.assertIn("алгоритм", results[0]["snippet"].lower())


class RoleAccessSmokeTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="teacher1",
            password="pass12345",
            is_staff=True,
        )
        Profile.objects.update_or_create(user=self.teacher, defaults={"role": Profile.ROLE_TEACHER})

        self.student_owner = User.objects.create_user(
            username="student_owner",
            password="pass12345",
        )
        self.student_other = User.objects.create_user(
            username="student_other",
            password="pass12345",
        )
        self.student_foreign = User.objects.create_user(
            username="student_foreign",
            password="pass12345",
        )

        group = Group.objects.create(name="QA-101", year=2026)
        Profile.objects.update_or_create(user=self.student_owner, defaults={"role": Profile.ROLE_STUDENT, "group": group})
        Profile.objects.update_or_create(user=self.student_other, defaults={"role": Profile.ROLE_STUDENT, "group": group})
        Profile.objects.update_or_create(user=self.student_foreign, defaults={"role": Profile.ROLE_STUDENT, "group": group})

        self.student_owner_row = Student.objects.create(
            user=self.student_owner,
            first_name="Owner",
            last_name="Student",
            email="owner@example.com",
            group=group,
        )
        self.student_other_row = Student.objects.create(
            user=self.student_other,
            first_name="Other",
            last_name="Student",
            email="other@example.com",
            group=group,
        )
        self.student_foreign_row = Student.objects.create(
            user=self.student_foreign,
            first_name="Foreign",
            last_name="Student",
            email="foreign@example.com",
            group=group,
        )

        self.course_owned = Course.objects.create(name="Owned course", code="OWN101", teacher=self.teacher)
        self.course_foreign = Course.objects.create(name="Foreign course", code="FRG101")
        self.lecture_owned = Lecture.objects.create(course=self.course_owned, title="Lecture 1", content_text="x")
        self.lecture_foreign = Lecture.objects.create(course=self.course_foreign, title="Lecture X", content_text="x")

        Enrollment.objects.create(student=self.student_owner_row, course=self.course_owned)
        Enrollment.objects.create(student=self.student_other_row, course=self.course_owned)
        Enrollment.objects.create(student=self.student_foreign_row, course=self.course_foreign)

    def test_student_cannot_open_foreign_lecture_detail(self):
        client = Client()
        client.login(username="student_owner", password="pass12345")
        resp = client.get(reverse("lecture_detail", kwargs={"pk": self.lecture_foreign.id}))
        self.assertEqual(resp.status_code, 302)

    def test_student_cannot_predict_for_foreign_course(self):
        client = Client()
        client.login(username="student_owner", password="pass12345")
        resp = client.post(reverse("predict_exam", kwargs={"course_id": self.course_foreign.id}), data={})
        self.assertEqual(resp.status_code, 302)

    def test_teacher_cannot_open_unrelated_student_profile(self):
        client = Client()
        client.login(username="teacher1", password="pass12345")
        resp = client.get(reverse("student_public_profile", kwargs={"pk": self.student_foreign_row.id}))
        self.assertEqual(resp.status_code, 302)



