"""
docindex.py — фрагменты документов для офлайн-ответов: откуда локальная модель берёт справку.

Онлайн вопрос раскладывает по подэтапам DeepSeek (rag.route_substages). Офлайн так нельзя —
это лишний вызов слабой модели на CPU. Поэтому содержательные секции заранее режем на
фрагменты ~900 символов, считаем вектор каждого (embed, bge-m3) и ищем гибридно:
по смыслу (косинус) + полнотекстово (Postgres, русская морфология), ранги сливаем (RRF).
Полнотекстовый поиск ловит точные термины и номера («СТО 53371127», «ПВТР»), векторы —
перефразы. Без сервера эмбеддингов работает только полнотекстовый.

Строится в онлайне после разбора документов (faq.refresh) и проверяется
scripts/offline_prepare.py. Секция пересобрана — её фрагменты удаляет каскад.
"""
import re
import threading

import numpy as np

import db
import embed
from redis_conn import get_redis

CHUNK_CHARS = 900
RRF_K = 60

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS doc_chunks (
        id          BIGSERIAL PRIMARY KEY,
        section_id  TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
        seq         INTEGER NOT NULL DEFAULT 0,
        text        TEXT NOT NULL,
        filename    TEXT NOT NULL DEFAULT '',
        substages   TEXT[] NOT NULL DEFAULT '{}',
        embedding   REAL[]
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_chunks_section ON doc_chunks(section_id)",
    "CREATE INDEX IF NOT EXISTS idx_doc_chunks_fts ON doc_chunks USING GIN (to_tsvector('russian', text))",
)

_lock = threading.Lock()
_idx = {"ver": None, "ids": np.zeros(0, dtype=np.int64), "mat": None}
_mem_ver = [0]


def init():
    for stmt in SCHEMA:
        db.execute(stmt)


def split(text: str, limit: int = CHUNK_CHARS) -> list:
    """Режем по абзацам, длинный абзац — по предложениям, совсем длинное — по limit."""
    out, cur = [], ""
    for para in [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]:
        parts = [para] if len(para) <= limit else re.split(r"(?<=[.!?;])\s+", para)
        for part in parts:
            while len(part) > limit:
                out.append(part[:limit])
                part = part[limit:]
            if cur and len(cur) + 1 + len(part) > limit:
                out.append(cur)
                cur = part
            else:
                cur = f"{cur}\n{part}" if cur else part
    if cur:
        out.append(cur)
    return out


def _version() -> int:
    r = get_redis()
    return _mem_ver[0] if r is None else int(r.get("nm:doc:ver") or 0)


def _bump():
    r = get_redis()
    if r is None:
        _mem_ver[0] += 1
    else:
        r.incr("nm:doc:ver")


def build() -> int:
    """Фрагменты и векторы для секций, у которых их ещё нет. Возвращает число новых."""
    rows = db.query(
        "SELECT s.id, s.text, l.substages, d.filename FROM sections s "
        "JOIN section_labels l ON l.section_id = s.id JOIN documents d ON d.id = s.doc_id "
        "WHERE l.is_meaningful AND length(s.text) > 0 "
        "AND NOT EXISTS (SELECT 1 FROM doc_chunks c WHERE c.section_id = s.id)") or []
    added = 0
    for r in rows:
        pieces = split(r["text"])
        vecs = embed.vectors(pieces) if embed.enabled() else None
        if embed.enabled() and vecs is None:
            break                          # сервер эмбеддингов недоступен — достроим позже
        subs = [s.get("id") for s in (r.get("substages") or []) if isinstance(s, dict) and s.get("id")]
        for i, piece in enumerate(pieces):
            db.execute("INSERT INTO doc_chunks (section_id, seq, text, filename, substages, embedding) "
                       "VALUES (%s,%s,%s,%s,%s,%s)",
                       (r["id"], i, piece, r["filename"], subs,
                        vecs[i].tolist() if vecs is not None else None))
        added += len(pieces)
    if added:
        _bump()
    return added


def stats() -> dict:
    row = db.query("SELECT count(*) AS chunks, count(embedding) AS with_vectors, "
                   "count(DISTINCT section_id) AS sections FROM doc_chunks", (), "one") or {}
    todo = db.query("SELECT count(*) AS n FROM sections s JOIN section_labels l ON l.section_id = s.id "
                    "WHERE l.is_meaningful AND length(s.text) > 0 AND NOT EXISTS "
                    "(SELECT 1 FROM doc_chunks c WHERE c.section_id = s.id)", (), "one") or {}
    return {**row, "sections_without_chunks": todo.get("n", 0)}


def _index() -> dict:
    ver = _version()
    with _lock:
        if _idx["ver"] != ver:
            rows = db.query("SELECT id, embedding FROM doc_chunks WHERE embedding IS NOT NULL "
                            "ORDER BY id") or []
            _idx["ids"] = np.asarray([r["id"] for r in rows], dtype=np.int64)
            _idx["mat"] = np.asarray([r["embedding"] for r in rows], dtype=np.float32) if rows else None
            _idx["ver"] = ver
        return dict(_idx)


def _fts_query(question: str) -> str:
    """Слова вопроса через ИЛИ: plainto_tsquery требует все слова — длинный вопрос не нашёл бы ничего."""
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", (question or "").lower()) if len(w) >= 3]
    return " | ".join(dict.fromkeys(words))


def rrf(rankings: list, k: int = RRF_K) -> list:
    """Слияние ранжирований: id, высоко стоящие в обоих списках, — первыми."""
    score = {}
    for ranking in rankings:
        for rank, i in enumerate(ranking):
            score[i] = score.get(i, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda i: -score[i])


def sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n+", text or "") if len(s.strip()) > 15]


def compress(question: str, texts: list, budget: int) -> str:
    """Ближайшие к вопросу предложения из texts в пределах budget символов, в исходном
    порядке. Слабая модель читает меньше и не теряется в соседних темах. Без сервера
    эмбеддингов — просто начало текстов."""
    sents = list(dict.fromkeys(s for t in texts for s in sentences(t)))
    if not sents or budget <= 0:
        return ""
    vecs = embed.vectors([question] + sents)
    if vecs is None:
        return "\n".join(sents)[:budget]
    scores = vecs[1:] @ vecs[0]
    keep, used = set(), 0
    for i in np.argsort(-scores):
        if used + len(sents[i]) + 1 > budget:
            continue
        keep.add(int(i))
        used += len(sents[i]) + 1
    return "\n".join(sents[i] for i in sorted(keep))


def search(question: str, k: int = 3, pool: int = 10) -> list:
    """k лучших фрагментов: [{'id', 'text', 'filename', 'substages'}]."""
    rankings = []
    idx = _index()
    if idx["mat"] is not None:
        vec = embed.vectors([question])
        if vec is not None:
            order = np.argsort(-(idx["mat"] @ vec[0]))[:pool]
            rankings.append([int(idx["ids"][i]) for i in order])
    tsq = _fts_query(question)
    if tsq:
        rows = db.query(
            "SELECT id FROM doc_chunks WHERE to_tsvector('russian', text) @@ to_tsquery('russian', %s) "
            "ORDER BY ts_rank(to_tsvector('russian', text), to_tsquery('russian', %s)) DESC LIMIT %s",
            (tsq, tsq, pool)) or []
        rankings.append([r["id"] for r in rows])
    ids = rrf(rankings)[:k]
    if not ids:
        return []
    rows = {r["id"]: r for r in (db.query(
        "SELECT id, text, filename, substages FROM doc_chunks WHERE id = ANY(%s)", (ids,)) or [])}
    return [{"id": i, "text": rows[i]["text"], "filename": rows[i]["filename"],
             "substages": list(rows[i]["substages"] or [])} for i in ids if i in rows]
