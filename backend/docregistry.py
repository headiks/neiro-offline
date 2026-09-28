"""
Реестр документов — операционное состояние файлов базы знаний, ключ = имя файла.

Что хранит одна запись: жизненный цикл (status: uploaded/processing/indexed/error,
error, chunks, phase, progress), происхождение (uploaded_by*, department, storage_path,
s3_key), результат классификации (folders, stage_ids, summary), уточнения человека
(clarification) и пути производных (path, markdown_path).

Отношение к таблице document_meta (documents.py): это РАЗНЫЕ роли. Здесь — жизненный
цикл и провенанс, ключ по имени файла; там — метаданные для доски «этапы ↔ документы»
(дедуп по sha256). Привязку к подэтапам даёт LLM-разметка docpipe.

Хранилище — таблица doc_registry в PostgreSQL (data JSONB). Обновление — атомарное
слияние полей (data || новые поля) одним UPDATE: параллельные воркеры не затирают
чужие поля, как это было с read-modify-write общего JSON-файла. Таблица живёт в схеме
кабинета, поэтому у каждого кабинета свой реестр. Прежний data/registry.json
переносится в БД один раз при старте (migrate_from_file).
"""

import json

from psycopg.types.json import Json

import db
from config import REGISTRY_PATH

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS doc_registry (
    filename    TEXT PRIMARY KEY,
    data        JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_doc_registry_status ON doc_registry ((data->>'status'))",
    "CREATE INDEX IF NOT EXISTS idx_doc_registry_uploaded ON doc_registry ((data->>'uploaded_at') DESC)",
    "CREATE INDEX IF NOT EXISTS idx_doc_registry_folders ON doc_registry USING GIN ((data->'folders'))",
    "CREATE INDEX IF NOT EXISTS idx_doc_registry_sha256 ON doc_registry ((data->>'sha256'))",
)


def _entry(row) -> dict:
    data = dict(row["data"] or {})
    data["filename"] = row["filename"]
    return data


def update(filename: str, **fields) -> dict:
    """Создать или дополнить запись (слияние полей). Возвращает запись целиком."""
    fields = {k: v for k, v in fields.items() if k != "filename"}
    row = db.query(
        "INSERT INTO doc_registry (filename, data) VALUES (%s, %s) "
        "ON CONFLICT (filename) DO UPDATE SET data = doc_registry.data || EXCLUDED.data, "
        "updated_at = now() RETURNING filename, data",
        (filename, Json({"filename": filename, **fields})), "one")
    return _entry(row)


def get(filename: str) -> dict | None:
    row = db.query("SELECT filename, data FROM doc_registry WHERE filename = %s", (filename,), "one")
    return _entry(row) if row else None


def find_by_sha256(digest: str) -> dict | None:
    """Документ с таким же содержимым (SHA-256) — дедупликация при загрузке."""
    row = db.query("SELECT filename, data FROM doc_registry WHERE data->>'sha256' = %s LIMIT 1",
                   (digest,), "one")
    return _entry(row) if row else None


def load() -> dict:
    """Весь реестр {имя файла: запись} — для совместимости со старым файловым интерфейсом."""
    return {d["filename"]: d for d in list_documents()}


def remove(filename: str) -> bool:
    row = db.query("DELETE FROM doc_registry WHERE filename = %s RETURNING filename", (filename,), "one")
    return row is not None


def list_documents() -> list:
    rows = db.query("SELECT filename, data FROM doc_registry "
                    "ORDER BY COALESCE(data->>'uploaded_at', '') DESC, filename") or []
    return [_entry(r) for r in rows]


def set_clarification(filename: str, text: str) -> bool:
    """Текстовое уточнение человека к документу (ТЗ §17). Сам документ не меняется —
    уточнение хранится в реестре как доп. контекст для ассистента."""
    row = db.query(
        "UPDATE doc_registry SET data = data || jsonb_build_object('clarification', %s::text), "
        "updated_at = now() WHERE filename = %s RETURNING filename", (text, filename), "one")
    return row is not None


def strip_folder(slug: str):
    """Убирает slug папки у всех документов (папку удалили/выключили). Сам документ
    остаётся в базе — снимается только принадлежность к категории (ТЗ §3)."""
    db.execute(
        "UPDATE doc_registry SET data = jsonb_set(data, '{folders}', "
        "  COALESCE((SELECT jsonb_agg(f) FROM jsonb_array_elements(data->'folders') f "
        "            WHERE f <> to_jsonb(%s::text)), '[]'::jsonb)), updated_at = now() "
        "WHERE data->'folders' ? %s", (slug, slug))


def folder_doc_counts() -> dict:
    """slug папки -> сколько документов к ней отнесено. Папка — логическая метка,
    документ может входить сразу в несколько папок."""
    rows = db.query(
        "SELECT f AS slug, count(*) AS n FROM doc_registry, "
        "jsonb_array_elements_text(COALESCE(data->'folders', '[]'::jsonb)) f GROUP BY f") or []
    return {r["slug"]: r["n"] for r in rows}


def init():
    db.execute(CREATE_TABLE)
    for stmt in CREATE_INDEXES:
        db.execute(stmt)


def migrate_from_file(path=None) -> int:
    """Разовый перенос data/registry.json в таблицу. Уже известные имена не трогаем
    (БД — источник правды), файл переименовывается в .migrated. -> число перенесённых."""
    path = path or REGISTRY_PATH
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return 0
    moved = 0
    for filename, entry in data.items():
        if not isinstance(entry, dict) or not filename:
            continue
        row = db.query("INSERT INTO doc_registry (filename, data) VALUES (%s, %s) "
                       "ON CONFLICT (filename) DO NOTHING RETURNING filename",
                       (filename, Json({**entry, "filename": filename})), "one")
        moved += 1 if row else 0
    path.replace(path.with_suffix(".json.migrated"))
    return moved
