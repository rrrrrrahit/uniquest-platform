import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Iterable, Set
import io

from django.db.models import QuerySet

from .models import Lecture


MODELS_DIR = Path("models")
FAISS_INDEX_PATH = MODELS_DIR / "faiss_index.bin"
FAISS_MAPPING_PATH = MODELS_DIR / "faiss_mapping.json"
EMBEDDINGS_INFO_PATH = MODELS_DIR / "embeddings_info.json"

_MODEL_CACHE: Dict[str, Any] = {}
_TOKEN_RE = re.compile(r"[\wа-яёА-ЯЁ]+", re.UNICODE)
_RU_STOP = frozenset(
    {
        "и",
        "в",
        "во",
        "на",
        "с",
        "со",
        "по",
        "для",
        "что",
        "как",
        "это",
        "а",
        "но",
        "или",
        "не",
        "о",
        "об",
        "от",
        "до",
        "из",
        "у",
        "к",
        "же",
        "ли",
        "бы",
        "все",
        "при",
        "так",
        "их",
        "чем",
        "уже",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
    }
)


def _load_embeddings_backend() -> Dict[str, Any]:
    if not EMBEDDINGS_INFO_PATH.exists():
        return {"backend": "bm25", "has_embeddings": False}
    try:
        return json.loads(EMBEDDINGS_INFO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"backend": "bm25", "has_embeddings": False}


def _faiss_ready() -> bool:
    info = _load_embeddings_backend()
    return (
        info.get("backend") == "faiss"
        and FAISS_INDEX_PATH.exists()
        and FAISS_MAPPING_PATH.exists()
    )


def _db_embeddings_ready() -> bool:
    info = _load_embeddings_backend()
    if not info.get("has_embeddings"):
        return False
    return Lecture.objects.exclude(vector_embedding__isnull=True).exists()


def _get_sentence_model(model_name: str):
    if model_name not in _MODEL_CACHE:
        from sentence_transformers import SentenceTransformer

        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]


def _encode_query(text: str):
    try:
        info = _load_embeddings_backend()
        model_name = info.get("model_name") or "sentence-transformers/all-MiniLM-L6-v2"
        model = _get_sentence_model(model_name)
        return model.encode([text])[0]
    except Exception:
        return None


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 2]


def _query_terms(query: str) -> List[str]:
    terms = _tokenize(query)
    return [t for t in terms if t not in _RU_STOP]


def _query_bigrams(terms: List[str]) -> List[str]:
    if len(terms) < 2:
        return []
    return [" ".join(terms[i : i + 2]) for i in range(len(terms) - 1)]


def _term_stem(term: str) -> str:
    """Грубый стем для русских словоформ (пузырьков → пузырьк)."""
    t = (term or "").strip().lower()
    if len(t) <= 4:
        return t
    for suffix in (
        "иями",
        "ями",
        "ами",
        "ого",
        "ему",
        "ыми",
        "ией",
        "ием",
        "ами",
        "ого",
        "ому",
        "ыми",
        "ать",
        "ять",
        "ить",
        "еть",
        "ов",
        "ев",
        "ом",
        "ем",
        "ам",
        "ям",
        "ах",
        "ях",
        "ую",
        "юю",
        "ой",
        "ей",
        "ий",
        "ый",
        "ая",
        "яя",
        "ое",
        "ее",
        "а",
        "я",
        "ы",
        "и",
        "у",
        "ю",
        "е",
        "о",
    ):
        if t.endswith(suffix) and len(t) - len(suffix) >= 4:
            return t[: -len(suffix)]
    return t[: max(4, len(t) - 2)]


def _haystack_has_term(haystack: str, term: str) -> bool:
    if not term:
        return False
    if term in haystack:
        return True
    stem = _term_stem(term)
    if len(stem) >= 4 and stem in haystack:
        return True
    if len(term) >= 4:
        for word in _TOKEN_RE.findall(haystack):
            if word.startswith(term[:4]) or term.startswith(word[:4]):
                return True
            if _term_stem(word) == stem:
                return True
    return False


def _haystack_term_count(haystack: str, term: str) -> int:
    if term in haystack:
        return haystack.count(term)
    stem = _term_stem(term)
    if len(stem) >= 4:
        return haystack.count(stem)
    return 0


def _ensure_lecture_texts(lectures: List[Lecture]) -> None:
    for lecture in lectures:
        _get_search_text(lecture)


def _rrf_merge(rank_lists: List[List[int]], k: int = 60) -> Dict[int, float]:
    """Reciprocal Rank Fusion — объединяет несколько ранжированных списков."""
    scores: Dict[int, float] = {}
    for ranks in rank_lists:
        if not ranks:
            continue
        for rank, lec_id in enumerate(ranks):
            scores[lec_id] = scores.get(lec_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _lecture_queryset(course_ids: Optional[Iterable[int]] = None) -> QuerySet:
    qs = Lecture.objects.select_related("course")
    if course_ids is not None:
        ids = [int(x) for x in course_ids]
        if not ids:
            return Lecture.objects.none()
        qs = qs.filter(course_id__in=ids)
    return qs


def semantic_search(
    query: str,
    top_k: int = 5,
    course_ids: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    """
    Поиск по лекциям: векторный индекс (если есть) + BM25 + текстовый ранг.
    """
    query = (query or "").strip()
    if not query:
        return []

    merged: Dict[int, Dict[str, Any]] = {}

    def _add_batch(batch: List[Dict[str, Any]], weight: float = 1.0):
        for item in batch:
            lec_id = item.get("id")
            if not lec_id:
                continue
            boosted = dict(item)
            boosted["score"] = float(item.get("score") or 0) * weight
            prev = merged.get(lec_id)
            if not prev or boosted["score"] > prev["score"]:
                merged[lec_id] = boosted

    vector_results: List[Dict[str, Any]] = []
    if _faiss_ready():
        vector_results = _faiss_search(query, top_k * 2, course_ids)
    elif _db_embeddings_ready():
        vector_results = _db_vector_search(query, top_k * 2, course_ids)

    if vector_results:
        _add_batch(vector_results, weight=1.0)

    if len(merged) < top_k:
        _add_batch(_bm25_search(query, top_k * 2, course_ids), weight=0.95)
    if len(merged) < top_k:
        _add_batch(_text_search_results(query, top_k * 2, course_ids), weight=0.9)
    if len(merged) < top_k:
        _add_batch(_lsa_semantic_search(query, top_k, course_ids), weight=0.85)

    ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:top_k]


def _faiss_search(
    query: str,
    top_k: int,
    course_ids: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    try:
        import faiss  # type: ignore
        import numpy as np

        info = _load_embeddings_backend()
        model_name = info.get("model_name") or "sentence-transformers/all-MiniLM-L6-v2"
        model = _get_sentence_model(model_name)

        index = faiss.read_index(str(FAISS_INDEX_PATH))
        mapping = json.loads(FAISS_MAPPING_PATH.read_text(encoding="utf-8"))

        q_vec = model.encode([query])[0].astype("float32")
        scores, indices = index.search(q_vec.reshape(1, -1), min(top_k * 3, len(mapping)))
        allowed = set(course_ids) if course_ids is not None else None

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(mapping):
                continue
            lec_id = mapping[idx]
            try:
                lec = Lecture.objects.select_related("course").get(id=lec_id)
            except Lecture.DoesNotExist:
                continue
            if allowed is not None and lec.course_id not in allowed:
                continue
            results.append(_lecture_to_result(lec, float(score), query))
            if len(results) >= top_k:
                break
        return results
    except Exception:
        return []


def _db_vector_search(
    query: str,
    top_k: int,
    course_ids: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    try:
        import numpy as np

        q_vec = _encode_query(query)
        if q_vec is None:
            return []

        lectures = _lecture_queryset(course_ids).exclude(vector_embedding__isnull=True)
        scored = []
        for lec in lectures:
            emb = lec.vector_embedding
            if not emb:
                continue
            v = np.array(emb, dtype=float)
            q = np.array(q_vec, dtype=float)
            denom = (np.linalg.norm(v) * np.linalg.norm(q)) or 1.0
            score = float(np.dot(v, q) / denom)
            if score > 0.05:
                scored.append((score, lec))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [_lecture_to_result(lec, score, query) for score, lec in scored[:top_k]]
    except Exception:
        return []


def _bm25_search(
    query: str,
    top_k: int,
    course_ids: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    try:
        from rank_bm25 import BM25Okapi  # type: ignore
    except Exception:
        return _text_search_results(query, top_k, course_ids)

    terms = _query_terms(query) or _tokenize(query)
    if not terms:
        return []

    lectures = list(_lecture_queryset(course_ids))
    corpus = []
    valid = []
    for lec in lectures:
        text = _get_search_text(lec)
        if not text:
            continue
        tokens = _tokenize(f"{lec.title} {text}")
        if not tokens:
            continue
        corpus.append(tokens)
        valid.append(lec)

    if not corpus:
        return []

    try:
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(terms)
        scored = sorted(
            [(float(score), lec) for score, lec in zip(scores, valid) if score > 0],
            key=lambda x: x[0],
            reverse=True,
        )
        if not scored:
            return _text_search_results(query, top_k, course_ids)
        max_score = scored[0][0] or 1.0
        return [
            _lecture_to_result(lec, min(100.0, (score / max_score) * 100.0), query)
            for score, lec in scored[:top_k]
        ]
    except Exception:
        return _text_search_results(query, top_k, course_ids)


def _text_search_results(
    query: str,
    top_k: int,
    course_ids: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    try:
        terms = _query_terms(query) or _tokenize(query)
        query_lower = query.lower()
        results = []

        for lec in _lecture_queryset(course_ids):
            title_lower = (lec.title or "").lower()
            content_lower = _get_search_text(lec).lower()
            if not content_lower and not title_lower:
                continue

            score_raw = 0.0
            if query_lower in title_lower:
                score_raw += 40.0
            if query_lower in content_lower:
                score_raw += 25.0

            for term in terms:
                if term in title_lower:
                    score_raw += 8.0
                score_raw += min(content_lower.count(term) * 2.0, 20.0)

            if score_raw <= 0:
                continue

            results.append(_lecture_to_result(lec, min(score_raw, 100.0), query))

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
    except Exception:
        return []


def _lsa_semantic_search(
    query: str,
    top_k: int,
    course_ids: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD
        from sklearn.metrics.pairwise import cosine_similarity
    except Exception:
        return []

    lectures = list(_lecture_queryset(course_ids))
    if not lectures:
        return []

    docs = []
    valid = []
    for lec in lectures:
        text = _get_search_text(lec)
        if not text:
            continue
        docs.append(f"{lec.title}\n\n{text}")
        valid.append(lec)

    if len(docs) < 2:
        return []

    try:
        vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            min_df=1,
            max_df=0.98,
            sublinear_tf=True,
        )
        doc_matrix = vectorizer.fit_transform(docs)
        query_matrix = vectorizer.transform([query])

        if doc_matrix.shape[0] < 3 or doc_matrix.shape[1] < 3:
            sims = cosine_similarity(query_matrix, doc_matrix).flatten()
        else:
            n_components = min(64, doc_matrix.shape[0] - 1, doc_matrix.shape[1] - 1)
            if n_components < 2:
                sims = cosine_similarity(query_matrix, doc_matrix).flatten()
            else:
                svd = TruncatedSVD(n_components=n_components, random_state=42)
                doc_lsa = svd.fit_transform(doc_matrix)
                query_lsa = svd.transform(query_matrix)
                sims = cosine_similarity(query_lsa, doc_lsa).flatten()

        ranked = sorted(
            [(float(score), lec) for score, lec in zip(sims, valid) if score > 0.01],
            key=lambda item: item[0],
            reverse=True,
        )[:top_k]
        return [_lecture_to_result(lec, min(score * 100.0, 100.0), query) for score, lec in ranked]
    except Exception:
        return []


def _lecture_to_result(lecture: Lecture, score: float, query: str = "") -> Dict[str, Any]:
    text = _get_search_text(lecture)
    snippet = _build_snippet(text, query)
    return {
        "id": lecture.id,
        "course_id": lecture.course_id,
        "title": lecture.title,
        "snippet": snippet,
        "url": lecture.content_url,
        "score": score,
    }


def _extract_text_from_lecture_file(lecture: Lecture) -> str:
    file_field = getattr(lecture, "lecture_file", None)
    if not file_field:
        return ""

    file_name = (getattr(file_field, "name", "") or "").lower()
    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    if not ext:
        return ""

    try:
        raw = file_field.read()
    except Exception:
        return ""
    finally:
        try:
            file_field.seek(0)
        except Exception:
            pass

    if not raw:
        return ""

    if ext in {"txt", "md", "csv", "json", "log", "py"}:
        for encoding in ("utf-8", "utf-8-sig", "cp1251", "latin1"):
            try:
                return raw.decode(encoding).strip()
            except Exception:
                continue
        return ""

    if ext == "pdf":
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(io.BytesIO(raw))
            pages = [(page.extract_text() or "") for page in reader.pages]
            return "\n".join(pages).strip()
        except Exception:
            return ""

    if ext == "docx":
        try:
            from docx import Document  # type: ignore

            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text).strip()
        except Exception:
            return ""

    return ""


def extract_lecture_text(lecture: Lecture) -> str:
    """Извлекает текст лекции из БД или из файла (PDF/DOCX/txt) и сохраняет в content_text."""
    return _get_search_text(lecture)


def _encode_document(text: str):
    """Вектор документа (заголовок + начало текста лекции)."""
    payload = (text or "").strip()
    if not payload:
        return None
    if len(payload) > 12000:
        payload = payload[:12000]
    return _encode_query(payload)


def prepare_lecture_for_search(lecture: Lecture) -> bool:
    """
    Подготавливает лекцию к поиску: извлекает текст из файла и при возможности
    сохраняет векторное представление.
    """
    text = extract_lecture_text(lecture).strip()
    if not text and not (lecture.title or "").strip():
        return False

    doc_for_vec = f"{lecture.title or ''}\n\n{text}".strip()
    update_fields: List[str] = []
    if text and (lecture.content_text or "").strip() != text:
        update_fields.append("content_text")

    try:
        doc_vec = _encode_document(doc_for_vec)
        if doc_vec is not None:
            lecture.vector_embedding = [float(x) for x in doc_vec]
            update_fields.append("vector_embedding")
    except Exception:
        pass

    if update_fields:
        lecture.save(update_fields=list(dict.fromkeys(update_fields)))
    return bool(text or (lecture.title or "").strip())


def _score_lecture_advanced(
    lecture: Lecture,
    query: str,
    terms: List[str],
    bigrams: List[str],
    q_lower: str,
) -> float:
    title = (lecture.title or "").strip()
    course_name = (getattr(lecture.course, "name", "") or "").strip()
    body = (lecture.content_text or "").strip() or _get_search_text(lecture)
    title_l = title.lower()
    body_l = body.lower()
    haystack = f"{title}\n{course_name}\n{body}".lower()

    if not haystack.strip():
        return 0.0

    score = 0.0
    if q_lower and q_lower in title_l:
        score += 55.0
    elif q_lower and q_lower in body_l:
        score += 32.0
    elif q_lower and q_lower in haystack:
        score += 20.0

    for bigram in bigrams:
        if bigram in haystack:
            score += 22.0

    if not terms and score > 0:
        return score

    matched_terms = 0
    for term in terms:
        term_hit = False
        if term in title_l:
            score += 14.0
            term_hit = True
        elif len(term) >= 4:
            for word in _TOKEN_RE.findall(title_l):
                if word.startswith(term) or term.startswith(word[: max(4, len(term))]):
                    score += 9.0
                    term_hit = True
                    break

        if _haystack_has_term(haystack, term):
            count = _haystack_term_count(haystack, term)
            score += min(max(count, 1) * 2.8, 22.0)
            term_hit = True
        if term_hit:
            matched_terms += 1

    if terms:
        coverage = matched_terms / len(terms)
        score += 28.0 * coverage

    if len(terms) >= 2:
        positions = []
        for term in terms:
            pos = haystack.find(term)
            if pos < 0:
                stem = _term_stem(term)
                if len(stem) >= 4:
                    pos = haystack.find(stem)
            if pos >= 0:
                positions.append(pos)
        if len(positions) >= 2 and max(positions) - min(positions) <= 150:
            score += 16.0

    return score


def _bm25_rank_lectures(query: str, lectures: List[Lecture], top_k: int) -> List[int]:
    try:
        from rank_bm25 import BM25Okapi  # type: ignore
    except Exception:
        return []

    terms = _query_terms(query) or _tokenize(query)
    if not terms or not lectures:
        return []

    corpus = []
    valid: List[Lecture] = []
    for lec in lectures:
        text = (lec.content_text or "").strip() or _get_search_text(lec)
        tokens = _tokenize(f"{lec.title} {text}")
        if not tokens:
            continue
        corpus.append(tokens)
        valid.append(lec)

    if not corpus:
        return []

    try:
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(terms)
        ranked = sorted(
            [(float(s), lec.id) for s, lec in zip(scores, valid) if s > 0],
            key=lambda x: x[0],
            reverse=True,
        )
        return [lec_id for _, lec_id in ranked[:top_k]]
    except Exception:
        return []


def _lsa_rank_lectures(query: str, lectures: List[Lecture], top_k: int) -> List[int]:
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD
        from sklearn.metrics.pairwise import cosine_similarity
    except Exception:
        return []

    docs = []
    ids: List[int] = []
    for lec in lectures:
        text = (lec.content_text or "").strip() or _get_search_text(lec)
        if not text and not (lec.title or "").strip():
            continue
        docs.append(f"{lec.title}\n\n{text}")
        ids.append(lec.id)

    if len(docs) < 2:
        return []

    try:
        vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            min_df=1,
            max_df=0.98,
            sublinear_tf=True,
        )
        doc_matrix = vectorizer.fit_transform(docs)
        query_matrix = vectorizer.transform([query])

        if doc_matrix.shape[0] < 3 or doc_matrix.shape[1] < 3:
            sims = cosine_similarity(query_matrix, doc_matrix).flatten()
        else:
            n_components = min(48, doc_matrix.shape[0] - 1, doc_matrix.shape[1] - 1)
            if n_components < 2:
                sims = cosine_similarity(query_matrix, doc_matrix).flatten()
            else:
                svd = TruncatedSVD(n_components=n_components, random_state=42)
                doc_lsa = svd.fit_transform(doc_matrix)
                query_lsa = svd.transform(query_matrix)
                sims = cosine_similarity(query_lsa, doc_lsa).flatten()

        ranked = sorted(
            [(float(score), lec_id) for score, lec_id in zip(sims, ids) if score > 0.02],
            key=lambda x: x[0],
            reverse=True,
        )
        return [lec_id for _, lec_id in ranked[:top_k]]
    except Exception:
        return []


def _vector_rank_lectures(
    query: str, lectures: List[Lecture], allowed_ids: Set[int], top_k: int
) -> List[int]:
    try:
        import numpy as np
    except Exception:
        return []

    q_vec = _encode_query(query)
    if q_vec is None:
        return []

    scored: List[tuple[float, int]] = []
    for lec in lectures:
        if lec.id not in allowed_ids:
            continue
        emb = lec.vector_embedding
        if not emb:
            continue
        v = np.array(emb, dtype=float)
        q = np.array(q_vec, dtype=float)
        denom = (np.linalg.norm(v) * np.linalg.norm(q)) or 1.0
        score = float(np.dot(v, q) / denom)
        if score > 0.04:
            scored.append((score, lec.id))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [lec_id for _, lec_id in scored[:top_k]]


def hybrid_search_for_lectures(
    query: str,
    lectures_qs: QuerySet,
    limit: int = 10,
    *,
    fast: bool = False,
) -> List[Dict[str, Any]]:
    """
    Гибридный поиск: BM25 + LSA + векторы + семантика + глубокий разбор текста файлов.
    """
    query = (query or "").strip()
    if not query:
        return []

    lectures = list(lectures_qs.select_related("course").order_by("-created_at")[:500])
    if not lectures:
        return []

    _ensure_lecture_texts(lectures)
    allowed_ids = {lec.id for lec in lectures}
    course_ids = list({lec.course_id for lec in lectures})
    id_to_lecture = {lec.id: lec for lec in lectures}

    terms = _query_terms(query) or _tokenize(query)
    bigrams = _query_bigrams(terms)
    q_lower = query.lower()

    rank_lists: List[List[int]] = []
    content_scores: Dict[int, float] = {}

    bm25_ranks = _bm25_rank_lectures(query, lectures, top_k=limit * 3)
    if bm25_ranks:
        rank_lists.append(bm25_ranks)

    advanced_ranked: List[tuple[float, int]] = []
    for lec in lectures:
        raw = _score_lecture_advanced(lec, query, terms, bigrams, q_lower)
        if raw > 0:
            advanced_ranked.append((raw, lec.id))
            content_scores[lec.id] = raw
    advanced_ranked.sort(key=lambda x: x[0], reverse=True)
    advanced_ranks = [lec_id for _, lec_id in advanced_ranked[: limit * 3]]
    if advanced_ranks:
        rank_lists.append(advanced_ranks)

    lsa_ranks = _lsa_rank_lectures(query, lectures, top_k=limit * 2)
    if lsa_ranks:
        rank_lists.append(lsa_ranks)

    if not fast:
        vector_ranks = _vector_rank_lectures(query, lectures, allowed_ids, top_k=limit * 2)
        if vector_ranks:
            rank_lists.append(vector_ranks)

        try:
            semantic_ranks = []
            for item in semantic_search(query, top_k=limit * 3, course_ids=course_ids):
                lec_id = item.get("id")
                if lec_id in allowed_ids:
                    semantic_ranks.append(lec_id)
            if semantic_ranks:
                rank_lists.append(semantic_ranks)
        except Exception:
            pass

    if not rank_lists:
        return []

    fused = _rrf_merge(rank_lists)
    max_rrf = max(fused.values()) or 1.0
    max_content = max(content_scores.values()) if content_scores else 1.0

    ordered_ids = sorted(fused.keys(), key=lambda i: fused[i], reverse=True)[:limit]
    results: List[Dict[str, Any]] = []
    for lec_id in ordered_ids:
        lecture = id_to_lecture.get(lec_id)
        if not lecture:
            continue
        rrf_part = (fused[lec_id] / max_rrf) * 70.0
        content_part = (content_scores.get(lec_id, 0) / max_content) * 30.0
        display_score = min(100.0, rrf_part + content_part)
        item = _lecture_to_result(lecture, display_score, query)
        item["lecture"] = lecture
        item["snippet"] = _build_snippet(
            (lecture.content_text or "").strip() or _get_search_text(lecture),
            query,
            max_len=360,
        )
        results.append(item)

    return results


def serialize_search_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Убирает ORM-объекты перед отдачей в JSON API."""
    clean: List[Dict[str, Any]] = []
    for item in results:
        row = {k: v for k, v in item.items() if k != "lecture"}
        clean.append(row)
    return clean


def search_lectures_by_content(
    query: str,
    lectures_qs: QuerySet,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Обратная совместимость: делегирует гибридному поиску."""
    return hybrid_search_for_lectures(query, lectures_qs, limit=limit)


def _get_search_text(lecture: Lecture) -> str:
    base = (lecture.content_text or "").strip()
    if base:
        return base

    extracted = _extract_text_from_lecture_file(lecture).strip()
    if extracted:
        lecture.content_text = extracted
        try:
            lecture.save(update_fields=["content_text"])
        except Exception:
            pass
        return extracted

    # Файл на диске мог пропасть (Render) — хотя бы ищем по названию.
    return (lecture.title or "").strip()


def build_lecture_snippet(lecture: Lecture, query: str = "", max_len: int = 320) -> str:
    text = _get_search_text(lecture)
    snippet = _build_snippet(text, query, max_len=max_len).strip()
    if snippet:
        return snippet
    if lecture.lecture_file:
        return f"Файл: {Path(lecture.lecture_file.name).name}"
    if lecture.content_url:
        return f"Ссылка: {lecture.content_url}"
    return "Текст фрагмента пока недоступен для этого материала."


def _build_snippet(text: str, query: str, max_len: int = 320) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    if not query:
        return text[:max_len] + ("..." if len(text) > max_len else "")

    text_lower = text.lower()
    query_lower = query.lower()
    terms = _tokenize(query)

    match_pos = text_lower.find(query_lower)
    if match_pos < 0:
        for term in terms:
            pos = text_lower.find(term)
            if pos >= 0:
                match_pos = pos
                break

    if match_pos < 0:
        return text[:max_len] + ("..." if len(text) > max_len else "")

    start = max(0, match_pos - max_len // 3)
    end = min(len(text), start + max_len)
    snippet = text[start:end].strip()

    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def search_backend_label(*, fast: bool = False) -> str:
    """Короткая подпись для UI: какой режим поиска активен."""
    if fast:
        return "текст файлов + BM25 + LSA"
    parts = ["гибрид: текст файлов", "BM25", "LSA"]
    if _faiss_ready():
        parts.append("FAISS")
    elif _db_embeddings_ready():
        parts.append("векторы")
    return " + ".join(parts)
