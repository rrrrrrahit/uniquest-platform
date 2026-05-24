from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from main.bootstrap import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    ensure_default_admin,
)


class Command(BaseCommand):
    help = "Создает администратора для входа в админку"

    def handle(self, *args, **options):
        existed = User.objects.filter(username=DEFAULT_ADMIN_USERNAME).exists()
        ensure_default_admin()

        if not existed:
            self.stdout.write(self.style.SUCCESS("Администратор создан успешно."))
        else:
            self.stdout.write(self.style.SUCCESS("Пароль администратора обновлен."))

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 50))
        self.stdout.write(self.style.SUCCESS("Данные для входа в админку:"))
        self.stdout.write(self.style.SUCCESS(f"Логин: {DEFAULT_ADMIN_USERNAME}"))
        self.stdout.write(self.style.SUCCESS(f"Пароль: {DEFAULT_ADMIN_PASSWORD}"))
        self.stdout.write(self.style.SUCCESS("=" * 50))
