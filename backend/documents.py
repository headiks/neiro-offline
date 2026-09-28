"""
documents.py — единый реестр метаданных загруженных документов в PostgreSQL.

Зачем (задача от руководителя):
  - одна БД «паспортов» файлов вместо файлового registry.json;
  - дедупликация по sha256: тот же файл не грузим и не переиндексируем дважды;
  - на этих метаданных строится экран «этапы/подэтапы ↔ документы».

Что хранит одна строка (таблица document_meta):
  sha256 (ключ) · имя · размер · тип · статус · когда/кем загружен ·
  краткое описание · ключевые слова · папки · этапы (stage_ids).

Привязка документа к подэтапам целиком отдана LLM-разметке docpipe (метки секций в
Postgres). Экран «этапы ↔ документы» собирает build_board() из этой разметки —
эмбеддингов, векторов и косинусной близости в системе больше нет.

Тяжёлые зависимости (db, psycopg) импортируются ВНУТРИ функций: чистая логика
(группировка экрана build_board) наверху тестируется без БД и сети.
"""

import time
from typing import Optional

# Имя таблицы РЕЕСТРА метаданных. НЕ "documents": так называется таблица пайплайна
# docpipe (со своей схемой и FK). Разводим по разным таблицам, чтобы обе жили рядом.
TABLE = "document_meta"

# ---------- Чистая логика (без БД и сети — тестируется отдельно) ----------
def build_board(stages: list, docs: list) -> dict:
    """
    Собирает данные экрана: этапы -> подэтапы -> документы + «без привязки».
    Чистая функция: на вход — этапы (stages.list_stages) и документы (docpipe-разметка).

    stages: [{id, title, description, substages:[{id, title}]}]
    docs:   [{sha256, filename, mime, keywords, stage_ids, substages:[{stage_id, substage_id, score}], ...}]
    """
    def ref(d: dict) -> dict:
        return {
            "sha256": d.get("sha256"), "filename": d.get("filename"),
            "mime": d.get("mime"), "keywords": d.get("keywords") or [],
            "status": d.get("status"),
        }

    # индекс: (stage_id, substage_id) -> [док], и (stage_id, None) -> прикреплён к этапу без подэтапа
    sub_docs: dict = {}
    stage_only: dict = {}
    assigned_ids = set()
    for d in docs:
        subs = d.get("substages") or []
        stage_ids = d.get("stage_ids") or []
        if subs:
            for a in subs:
                sub_docs.setdefault((a["stage_id"], a["substage_id"]), []).append({**ref(d), "score": a.get("score")})
                assigned_ids.add(d.get("sha256"))
        # этапы, куда документ отнесён, но без конкретного подэтапа
        subs_stage_ids = {a["stage_id"] for a in subs}
        for sid in stage_ids:
            if sid not in subs_stage_ids:
                stage_only.setdefault(sid, []).append(ref(d))
                assigned_ids.add(d.get("sha256"))

    out_stages = []
    sub_count = 0
    for st in stages:
        subs_out = []
        for sub in st.get("substages") or []:
            sub_count += 1
            subs_out.append({
                "id": sub["id"], "title": sub.get("title", ""),
                "documents": sub_docs.get((st["id"], sub["id"]), []),
            })
        out_stages.append({
            "id": st["id"], "title": st.get("title", ""),
            "description": st.get("description", ""),
            "substages": subs_out,
            "documents": stage_only.get(st["id"], []),   # в этапе, но без подэтапа
        })

    unassigned = [ref(d) for d in docs if d.get("sha256") not in assigned_ids]
    return {
        "stages": out_stages,
        "unassigned": unassigned,
        "stats": {
            "stages": len(stages), "substages": sub_count,
            "documents": len(docs), "unassigned": len(unassigned),
        },
    }


# ---------- Хранилище (PostgreSQL) ----------
CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    sha256       TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    size_bytes   BIGINT,
    mime         TEXT,
    status       TEXT NOT NULL DEFAULT 'indexed',
    uploaded_at  TEXT,
    uploaded_by  TEXT,
    summary      TEXT,
    keywords     TEXT[],
    embedding    REAL[],
    folders      TEXT[],
    stage_ids    TEXT[],
    substages    JSONB DEFAULT '[]'::jsonb,
    updated_at   TEXT
)
"""
# Быстрый поиск «документы этапа» для экрана.
CREATE_INDEX = f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_stage ON {TABLE} USING GIN (stage_ids)"
# remove_by_filename и сверка при загрузке новой версии — по имени файла.
CREATE_INDEX_NAME = f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_filename ON {TABLE}(filename)"


def init():
    """Создаёт таблицу и индекс, если их ещё нет. Идемпотентно — звать при старте."""
    import db
    db.execute(CREATE_TABLE)
    db.execute(CREATE_INDEX)
    db.execute(CREATE_INDEX_NAME)


def find_by_hash(sha256: str) -> Optional[dict]:
    """Дедупликация: строка документа с таким содержимым или None. Главная проверка
    перед повторной загрузкой — «этот файл уже есть»."""
    import db
    return db.query(f"SELECT * FROM {TABLE} WHERE sha256 = %s", (sha256,), "one")


def get(sha256: str) -> Optional[dict]:
    import db
    return db.query(f"SELECT * FROM {TABLE} WHERE sha256 = %s", (sha256,), "one")


def list_meta() -> list:
    """Строки для табличного интерфейса — все поля, КРОМЕ тяжёлого вектора (у него
    отдаём только длину). Иначе на каждый документ ехало бы по 1024 числа."""
    import db
    return db.query(
        f"""SELECT sha256, filename, size_bytes, mime, status, uploaded_at, uploaded_by,
                   summary, keywords, folders, stage_ids, substages, updated_at,
                   array_length(embedding, 1) AS embedding_dim
            FROM {TABLE} ORDER BY uploaded_at DESC NULLS LAST""", (), "all")


def remove(sha256: str) -> None:
    import db
    db.execute(f"DELETE FROM {TABLE} WHERE sha256 = %s", (sha256,))


def remove_by_filename(filename: str) -> None:
    """Убрать строку по имени файла — при удалении документа из базы знаний."""
    import db
    db.execute(f"DELETE FROM {TABLE} WHERE filename = %s", (filename,))


def hash_bytes(content: bytes) -> str:
    """sha256-«отпечаток» содержимого (первые 16 hex). Тот же формат, что у
    indexing.file_hash, чтобы дедуп по хэшу совпадал с обеих сторон."""
    import hashlib
    return hashlib.sha256(content).hexdigest()[:16]


_UPSERT = f"""
INSERT INTO {TABLE}
    (sha256, filename, size_bytes, mime, status, uploaded_at, uploaded_by,
     summary, keywords, embedding, folders, stage_ids, substages, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (sha256) DO UPDATE SET
    filename=EXCLUDED.filename, size_bytes=EXCLUDED.size_bytes, mime=EXCLUDED.mime,
    status=EXCLUDED.status, uploaded_at=EXCLUDED.uploaded_at, uploaded_by=EXCLUDED.uploaded_by,
    summary=EXCLUDED.summary, keywords=EXCLUDED.keywords, embedding=EXCLUDED.embedding,
    folders=EXCLUDED.folders, stage_ids=EXCLUDED.stage_ids, substages=EXCLUDED.substages,
    updated_at=EXCLUDED.updated_at
"""


def upsert(meta: dict) -> dict:
    """Создаёт/обновляет строку документа по sha256. meta — поля таблицы (см. classify_full)."""
    import db
    from psycopg.types.json import Json
    db.execute(_UPSERT, (
        meta["sha256"], meta.get("filename", ""), meta.get("size_bytes"), meta.get("mime"),
        meta.get("status", "indexed"), meta.get("uploaded_at"), meta.get("uploaded_by"),
        meta.get("summary"), meta.get("keywords") or [], meta.get("embedding"),
        meta.get("folders") or [], meta.get("stage_ids") or [],
        Json(meta.get("substages") or []), time.strftime("%Y-%m-%dT%H:%M:%S"),
    ))
    return get(meta["sha256"])


# Привязка документов к подэтапам целиком отдана LLM-разметке docpipe (метки секций в
# Postgres, доска строится из них). Прежние функции classify_full/record/_substage_vectors,
# считавшие эмбеддинги документа и косинусную близость к подэтапам, удалены вместе с
# векторным стором. Экран «этапы↔документы» собирает build_board() из docpipe-разметки.


