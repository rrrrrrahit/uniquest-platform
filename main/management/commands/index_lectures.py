import json
from pathlib import Path

from django.core.management.base import BaseCommand

from main.models import Lecture
from main.search_service import _get_search_text


class Command(BaseCommand):
    help = "Строит эмбеддинги для лекций и, при возможности, индекс для поиска (pgvector/FAISS/BM25)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model-name",
            type=str,
            default="sentence-transformers/all-MiniLM-L6-v2",
            help="Имя модели sentence-transformers (если доступна)",
        )

    def handle(self, *args, **options):
        model_name = options["model_name"]

        # Путь для файлов индекса
        base = Path("models")
        base.mkdir(exist_ok=True, parents=True)
        mapping_path = base / "faiss_mapping.json"
        info_path = base / "embeddings_info.json"

        self.stdout.write(self.style.MIGRATE_HEADING("=== Индексация лекций ==="))

        lectures = Lecture.objects.all()
        if not lectures.exists():
            self.stdout.write(self.style.WARNING("Лекций в базе нет."))
            return

        # Пытаемся загрузить sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(model_name)
            use_embeddings = True
        except Exception as exc:  # pragma: no cover - модель может быть недоступна
            self.stderr.write(
                self.style.WARNING(
                    f"Не удалось загрузить модель sentence-transformers ({exc}). "
                    f"Будет доступен только текстовый BM25-поиск."
                )
            )
            use_embeddings = False
            model = None

        indexed = []
        for lec in lectures:
            text = _get_search_text(lec).strip()
            if text:
                indexed.append((lec, text))

        if not indexed:
            self.stdout.write(
                self.style.WARNING(
                    "Нет лекций с извлекаемым текстом (файл или content_text). "
                    "Поиск будет работать по заголовкам через BM25."
                )
            )

        texts = [text for _, text in indexed]
        ids = [lec.id for lec, _ in indexed]

        if use_embeddings and texts:
            self.stdout.write("Генерация эмбеддингов для лекций...")
            embeddings = model.encode(texts, show_progress_bar=False)

            # Сохраняем в БД (поле vector_embedding)
            for (lec, _), emb in zip(indexed, embeddings):
                lec.vector_embedding = list(map(float, emb))
                lec.save(update_fields=["vector_embedding"])

            # Пытаемся построить FAISS-индекс
            try:
                import faiss  # type: ignore

                dim = embeddings.shape[1]
                index = faiss.IndexFlatIP(dim)
                index.add(embeddings.astype("float32"))
                faiss.write_index(index, str(base / "faiss_index.bin"))

                mapping_path.write_text(json.dumps(ids), encoding="utf-8")
                backend = "faiss"
                self.stdout.write(self.style.SUCCESS("FAISS-индекс успешно создан."))
            except Exception as exc:  # pragma: no cover
                self.stderr.write(
                    self.style.WARNING(
                        f"Не удалось создать FAISS-индекс ({exc}). Будет использоваться поиск по БД."
                    )
                )
                backend = "database"
        else:
            backend = "bm25"

        has_vectors = use_embeddings and bool(texts)
        info = {
            "backend": backend,
            "n_lectures": len(indexed),
            "has_embeddings": has_vectors,
            "model_name": model_name if has_vectors else None,
        }
        info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")

        self.stdout.write(
            self.style.SUCCESS(
                f"Индексация завершена. Backend={backend}, лекций={lectures.count()}."
            )
        )



