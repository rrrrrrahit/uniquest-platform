from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('setup-demo-admin/', views.setup_demo_admin_view, name='setup_demo_admin'),
    path('bootstrap-admin/', views.setup_demo_admin_view, name='bootstrap_admin'),
    path('logout/', views.logout_view, name='logout'),
    # Алиасы стандартных django-auth URL, чтобы исключить 404 на /accounts/*
    path('accounts/login/', views.login_view),
    path('accounts/register/', views.register_view),
    path('create-test-student/', views.create_test_student_view, name='create_test_student'),
    path('create-test-teacher/', views.create_test_teacher_view, name='create_test_teacher'),

    path('dashboard/', views.dashboard, name='dashboard'),
    path('student-passport/', views.student_passport_view, name='student_passport'),
    path('profile/', views.profile_view, name='profile'),
    path('schedule/', views.schedule_view, name='schedule'),
    path('grades/', views.grades_view, name='grades'),
    # ai-assistant/ зарегистрирован в uniquest/urls.py (корень проекта)
    path('ai-learning-assistant/', views.ai_learning_assistant, name='ai_learning_assistant'),
    path('ai-learning-assistant/predict/<int:course_id>/', views.predict_exam_view, name='predict_exam'),
    path('ai-learning-assistant/plan/<int:course_id>/', views.create_study_plan_view, name='create_study_plan'),

    path('course/<int:pk>/', views.course_detail, name='course_detail'),
    path('quizzes/<int:quiz_id>/take/', views.take_quiz_view, name='take_quiz'),

    path('teacher/dashboard/', views.teacher_dashboard, name='teacher_dashboard'),
    path('teacher/courses/', views.teacher_courses, name='teacher_courses'),
    path('teacher/disciplines/<int:pk>/', views.teacher_discipline_detail, name='teacher_discipline_detail'),
    path('teacher/disciplines/<int:pk>/generate-quiz/', views.generate_lecture_quiz_view, name='generate_lecture_quiz'),
    path('teacher/grades/', views.teacher_grades, name='teacher_grades'),
    path('teacher/schedule/', views.teacher_schedule, name='teacher_schedule'),
    path('teacher/students/<int:student_id>/analysis/', views.teacher_student_analysis, name='teacher_student_analysis'),
    path('teacher/ai-analysis/<int:student_id>/<int:course_id>/', views.ai_analysis_view, name='ai_analysis'),

    # Публичные академические страницы
    path('groups/', views.groups_list, name='groups_list'),
    path('groups/<int:group_id>/schedule/', views.group_schedule, name='group_schedule'),
    path('students/<int:pk>/profile/', views.student_public_profile, name='student_public_profile'),
    path('courses/<int:pk>/lectures/', views.course_lectures, name='course_lectures'),
    path('lectures/<int:pk>/', views.lecture_detail, name='lecture_detail'),
    path('lectures/<int:pk>/download/', views.download_lecture_file, name='download_lecture_file'),
    path('demo/', views.demo_page, name='demo_page'),

    # API для ML
    path('api/predict_grade/', views.api_predict_grade, name='api_predict_grade'),
    path('api/search_resources/', views.api_search_resources, name='api_search_resources'),
    path('api/retrain_embeddings/', views.api_retrain_embeddings, name='api_retrain_embeddings'),
]
