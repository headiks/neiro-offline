"""
qacache.py — база готовых ответов: похожий вопрос получает ответ мгновенно, без модели.

Откуда ответы (source):
  faq     — топ-10 вопросов по подэтапу, пишет DeepSeek по документам (faq.py);
  section — 2 вопроса по каждой секции документа (faq.py);
  model   — ответ ассистента на вопрос сотрудника (rag.handle_question);
  human   — ответ специалиста из очереди «Вопросы», если админ добавил его в базу.

Поиск: сначала точное совпадение набора значимых слов (normalize), затем по смыслу —
косинус вектора вопроса (embed.py, bge-m3) не ниже NEIROMASTER_QA_SIM. Порог осторожный:
на замере разные по смыслу вопросы давали до 0,80, одинаковые — от 0,68; ниже порога вопрос
идёт обычным путём (модель). Своя должность — небольшой бонус к похожести, общие ответы
(position='') видны всем.

Хранение: таблица qa_answers в основной БД — переживает перезапуски, без TTL. Вопрос и ответ
шифруются как ПДн (сотрудник мог написать в вопросе своё имя). Изменились документы — faq.py
удаляет ответы faq/model по затронутым подэтапам и пишет новые; ответы специалистов не трогает.
Векторы держим в памяти процесса; любая запись увеличивает версию (Redis nm:qa:ver), и
процессы перечитывают матрицу.
ponytail: полная перезагрузка матрицы после каждой записи; тысячи строк — доли секунды.
Десятки тысяч — догружать только новые id.
"""
import hashlib
import json
import os
import re
import threading

import numpy as np

import db
import embed
import users
from redis_conn import get_redis

# Версия формата ответа: меняли промпт ответа (rag.GENERATE_SYSTEM) — увеличьте, и старые
# ответы модели удалятся при старте. 3 — без ссылок на документы и устройство системы.
ANSWER_FORMAT = 3
SOURCES = ("faq", "section", "model", "human")
POS_BONUS = 0.02

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS qa_answers (
        id          BIGSERIAL PRIMARY KEY,
        norm_hash   TEXT NOT NULL,
        question    TEXT NOT NULL,
        answer      TEXT NOT NULL,
        embedding   REAL[],
        source      TEXT NOT NULL,
        position    TEXT NOT NULL DEFAULT '',
        substages   TEXT[] NOT NULL DEFAULT '{}',
        section_id  TEXT,
        meta        JSONB NOT NULL DEFAULT '{}',
        format      INTEGER NOT NULL DEFAULT 0,
        hits        INTEGER NOT NULL DEFAULT 0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_qa_answers_norm ON qa_answers(norm_hash)",
    "CREATE INDEX IF NOT EXISTS idx_qa_answers_substages ON qa_answers USING GIN (substages)",
    "CREATE INDEX IF NOT EXISTS idx_qa_answers_section ON qa_answers(section_id)",
    # Что уже сгенерировано faq.py: ключ «sub:<подэтап>» / «sec:<секция>» -> отпечаток входов.
    """
    CREATE TABLE IF NOT EXISTS qa_state (
        key         TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)

# Служебные слова, не меняющие смысла вопроса. Отрицания («не», «ни», «нельзя») сюда НЕ
# входят: «Мне выдали пропуск?» и «Мне не выдали пропуск» — разные вопросы с разными ответами.
_STOP = {"а", "в", "во", "и", "к", "ко", "на", "о", "об", "от", "по", "с", "со", "у", "за", "из",
         "для", "до", "ли", "же", "бы", "то", "это", "мне", "я", "мы", "мой", "моя",
         "мои", "нам", "нас", "меня", "как", "подскажите", "скажите", "пожалуйста", "можно"}

_lock = threading.Lock()
_idx = {"ver": None, "ids": np.zeros(0, dtype=np.int64), "pos": [], "mat": None}
_mem_ver = [0]    # версия без Redis (один процесс)


def sim_threshold() -> float:
    return float(os.environ.get("NEIROMASTER_QA_SIM", "0.84"))


def init():
    for stmt in SCHEMA:
        db.execute(stmt)
    # Сменился формат ответа — ответы модели и FAQ устарели; FAQ пересоздаст faq.refresh.
    db.execute("DELETE FROM qa_answers WHERE source <> 'human' AND format <> %s", (ANSWER_FORMAT,))
    db.execute("DELETE FROM qa_state WHERE fingerprint NOT LIKE %s", (f"{ANSWER_FORMAT}:%",))


def normalize(question: str) -> str:
    text = (question or "").lower().replace("ё", "е")
    words = [w for w in re.findall(r"[a-zа-я0-9]+", text) if w not in _STOP]
    return " ".join(sorted(set(words)))


def _norm_hash(question: str) -> str:
    norm = normalize(question)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest() if norm else ""


def _pos(position: str) -> str:
    return (position or "").strip().lower()


# ---------- Версия и матрица векторов ----------
def _version() -> int:
    r = get_redis()
    return _mem_ver[0] if r is None else int(r.get("nm:qa:ver") or 0)


def _bump():
    r = get_redis()
    if r is None:
        _mem_ver[0] += 1
    else:
        r.incr("nm:qa:ver")


def _index() -> dict:
    ver = _version()
    with _lock:
        if _idx["ver"] != ver:
            rows = db.query("SELECT id, position, embedding FROM qa_answers "
                            "WHERE embedding IS NOT NULL ORDER BY id") or []
            _idx["ids"] = np.asarray([r["id"] for r in rows], dtype=np.int64)
            _idx["pos"] = [r["position"] for r in rows]
            _idx["mat"] = np.asarray([r["embedding"] for r in rows], dtype=np.float32) if rows else None
            _idx["ver"] = ver
        return dict(_idx)


def best_match(mat, ids, positions, vec, position: str = ""):
    """(id, похожесть) лучшего ответа, видимого этой должности (свои + общие), или None.
    Своя должность получает POS_BONUS: при равной похожести выигрывает ответ под неё."""
    if mat is None or not len(ids):
        return None
    pos = _pos(position)
    scores = mat @ vec
    own = np.asarray([p == pos for p in positions])
    visible = own | np.asarray([p == "" for p in positions])
    if not visible.any():
        return None
    scores = np.where(visible, scores + own * (POS_BONUS if pos else 0.0), -1.0)
    i = int(np.argmax(scores))
    return int(ids[i]), float(scores[i])


def nearest(question: str, position: str = "", k: int = 3) -> list:
    """k ближайших готовых пар вопрос–ответ (для подсказок «похожие вопросы» и контекста
    слабой локальной модели). Без сервера эмбеддингов — пусто."""
    idx = _index()
    vec = embed.vectors([question]) if idx["mat"] is not None else None
    if vec is None:
        return []
    pos = _pos(position)
    scores = idx["mat"] @ vec[0]
    order = [i for i in np.argsort(-scores) if idx["pos"][i] in ("", pos)][:k]
    out = []
    for i in order:
        row = db.query("SELECT * FROM qa_answers WHERE id = %s", (int(idx["ids"][i]),), "one")
        if row:
            out.append({**_public(row), "score": round(float(scores[i]), 3)})
    return out


# ---------- Ответ по вопросу ----------
def _public(row: dict) -> dict:
    return {"id": row["id"], "question": users._decrypt_field(row["question"]),
            "answer": users._decrypt_field(row["answer"]), "source": row["source"],
            "position": row["position"], "substages": list(row.get("substages") or []),
            "hits": row.get("hits", 0), "meta": row.get("meta") or {},
            "created_at": str(row.get("created_at") or ""), "updated_at": str(row.get("updated_at") or "")}


def get(question: str, position: str = ""):
    """Готовый ответ: {'answer', 'qa_id', 'qa_source', ...meta} или None."""
    h = _norm_hash(question)
    if not h:
        return None
    pos = _pos(position)
    row = db.query(
        "SELECT * FROM qa_answers WHERE norm_hash = %s AND position IN (%s, '') "
        "ORDER BY (position = %s) DESC, (source = 'human') DESC, id DESC LIMIT 1",
        (h, pos, pos), "one")
    score = 1.0
    if row is None and embed.enabled():
        idx = _index()
        vec = embed.vectors([question]) if idx["mat"] is not None else None
        hit = best_match(idx["mat"], idx["ids"], idx["pos"], vec[0], pos) if vec is not None else None
        if hit and hit[1] >= sim_threshold():
            row = db.query("SELECT * FROM qa_answers WHERE id = %s", (hit[0],), "one")
            score = hit[1]
    if not row:
        return None
    db.execute("UPDATE qa_answers SET hits = hits + 1 WHERE id = %s", (row["id"],))
    meta = row.get("meta") or {}
    return {**meta, "answer": users._decrypt_field(row["answer"]), "qa_id": row["id"],
            "qa_source": row["source"], "qa_score": round(score, 3)}


def add_many(items: list, source: str) -> int:
    """items: [{'question', 'answer', 'position'?, 'substages'?, 'section_id'?, 'meta'?}].
    Векторы — одним пакетом. Возвращает число записанных."""
    if source not in SOURCES:
        raise ValueError(f"Неизвестный источник ответа: {source}")
    items = [it for it in items if _norm_hash(it.get("question")) and (it.get("answer") or "").strip()]
    if not items:
        return 0
    vecs = embed.vectors([it["question"] for it in items])
    for i, it in enumerate(items):
        db.execute(
            "INSERT INTO qa_answers (norm_hash, question, answer, embedding, source, position, "
            "substages, section_id, meta, format) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (_norm_hash(it["question"]), users._encrypt_field(it["question"].strip()),
             users._encrypt_field(it["answer"].strip()),
             vecs[i].tolist() if vecs is not None else None, source, _pos(it.get("position")),
             list(it.get("substages") or []), it.get("section_id"),
             json.dumps(it.get("meta") or {}, ensure_ascii=False), ANSWER_FORMAT))
    _bump()
    return len(items)


def put(question: str, position: str, payload: dict, source: str = "model", substages=()):
    """Ответ ассистента/специалиста в базу. payload — {'answer', ...служебное в meta}."""
    meta = {k: v for k, v in (payload or {}).items() if k != "answer"}
    return add_many([{"question": question, "answer": (payload or {}).get("answer") or "",
                      "position": position, "substages": list(substages or []),
                      "meta": meta}], source)


def delete_for_substages(substage_ids, sources=("faq", "model")) -> int:
    """Документы подэтапов изменились — ответы, построенные на них, больше не верны."""
    ids = list(substage_ids or [])
    if not ids:
        return 0
    rows = db.query("DELETE FROM qa_answers WHERE source = ANY(%s) AND substages && %s::text[] "
                    "RETURNING id", (list(sources), ids)) or []
    if rows:
        _bump()
    return len(rows)


# ---------- Администрирование ----------
def list_all(source: str = "", limit: int = 5000) -> list:
    where, params = "", []
    if source:
        where, params = "WHERE source = %s", [source]
    rows = db.query(f"SELECT * FROM qa_answers {where} ORDER BY hits DESC, id DESC LIMIT %s",
                    tuple(params + [max(1, min(int(limit), 20000))])) or []
    return [_public(r) for r in rows]


def stats() -> dict:
    rows = db.query("SELECT source, count(*) AS n FROM qa_answers GROUP BY source") or []
    return {r["source"]: r["n"] for r in rows}


def update(qa_id: int, question: str, answer: str, editor: str = ""):
    """Правка админом: новый текст, новый вектор; ответ становится «от специалиста» — его
    больше не удалит автообновление FAQ."""
    question, answer = (question or "").strip(), (answer or "").strip()
    if not _norm_hash(question) or not answer:
        raise ValueError("Нужны вопрос и ответ")
    vec = embed.vectors([question])
    row = db.query(
        "UPDATE qa_answers SET norm_hash = %s, question = %s, answer = %s, embedding = %s, "
        "source = 'human', meta = meta || %s::jsonb, updated_at = now() WHERE id = %s RETURNING *",
        (_norm_hash(question), users._encrypt_field(question), users._encrypt_field(answer),
         vec[0].tolist() if vec is not None else None,
         json.dumps({"edited_by": editor}, ensure_ascii=False), int(qa_id)), "one")
    if row:
        _bump()
    return _public(row) if row else None


def delete(qa_id: int) -> bool:
    row = db.query("DELETE FROM qa_answers WHERE id = %s RETURNING id", (int(qa_id),), "one")
    if row:
        _bump()
    return bool(row)


def reembed_missing() -> int:
    """Векторы для ответов, записанных, пока сервер эмбеддингов был недоступен."""
    rows = db.query("SELECT id, question FROM qa_answers WHERE embedding IS NULL") or []
    if not rows or not embed.enabled():
        return 0
    vecs = embed.vectors([users._decrypt_field(r["question"]) for r in rows])
    if vecs is None:
        return 0
    for r, v in zip(rows, vecs):
        db.execute("UPDATE qa_answers SET embedding = %s WHERE id = %s", (v.tolist(), r["id"]))
    _bump()
    return len(rows)
