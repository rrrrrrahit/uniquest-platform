import os
import sys

from django.apps import AppConfig
from django.db.utils import OperationalError, ProgrammingError


class MainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "main"

    def ready(self):
        import main.signals  # noqa: F401

        skip_commands = {
            "migrate",
            "makemigrations",
            "collectstatic",
            "test",
            "shell",
            "createsuperuser",
            "create_admin",
        }
        if skip_commands.intersection(set(sys.argv[1:2])):
            return

        # runserver autoreload: только дочерний процесс
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return

        try:
            from main.bootstrap import ensure_default_admin

            ensure_default_admin()
        except (OperationalError, ProgrammingError):
            # Таблицы auth_user ещё не созданы (до migrate)
            pass
