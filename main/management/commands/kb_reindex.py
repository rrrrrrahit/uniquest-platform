"""Извлекает текст из файлов лекций в content_text для поиска."""

from django.core.management.base import BaseCommand

from main.kb import lecture_body
from main.models import Lecture


class Command(BaseCommand):
    help = "Извлечь текст из PDF/DOCX всех лекций в поле content_text."

    def handle(self, *args, **options):
        total = 0
        for lec in Lecture.objects.select_related("course").iterator():
            text = lecture_body(lec)
            if text:
                total += 1
        self.stdout.write(self.style.SUCCESS(f"Готово. Лекций с текстом: {total}."))
