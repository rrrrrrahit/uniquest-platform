from django.apps import AppConfig


class MainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "main"

    def ready(self):
        # Только подключение сигналов. БД в ready() не трогаем (Django 5.2 RuntimeWarning).
        # admin на Render: manage.py create_admin в startCommand и ensure_default_admin в wsgi.py
        import main.signals  # noqa: F401
