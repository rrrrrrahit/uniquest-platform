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

        if os.environ.get("RENDER") != "true" and not os.environ.get(
            "RENDER_EXTERNAL_HOSTNAME"
        ):
            return

        try:
            import logging

            from main.bootstrap import ensure_default_admin

            ensure_default_admin()
        except (OperationalError, ProgrammingError):
            pass
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "ensure_default_admin in ready() failed: %s", exc
            )
