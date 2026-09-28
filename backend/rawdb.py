"""
rawdb.py — отдельная PostgreSQL для ИСХОДНЫХ данных: оригиналы документов и загруженные
штатные расписания — байт в байт, как их загрузили.

Две базы по назначению:
  raw       — исходники (эта). Меняется только загрузкой и удалением. Из неё всегда можно
              заново получить всё остальное — пакетной переобработкой.
  processed — всё, что получено обработкой (разделы, разметка, планы, сообщения), и рабочие
              данные приложения (пользователи, сессии, журнал). Это основная БД — db.py.

Подключение — NEIROMASTER_RAW_DB_DSN. Не задано — raw-хранилище выключено (как S3):
оригиналы живут только в data/documents/, приложение работает как раньше.

Штатка содержит ФИО — хранится зашифрованной ключом ПДн (тот же Fernet, что у полей
пользователей). Документы — как есть: они и раньше лежали открыто на диске и в S3.

Перенос уже загруженных документов в raw-БД:  python rawdb.py --backfill
"""
import hashlib
import os
import threading

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DSN = os.environ.get("NEIROMASTER_RAW_DB_DSN", "").strip()
POOL_MAX = int(os.environ.get("NEIROMASTER_RAW_DB_POOL", "4"))

SCHEMA = (
    # key — ключ хранилища (storage.doc_key): <суперадмин>/<админ>/<файл> или плоский.
    """
    CREATE TABLE IF NOT EXISTS raw_documents (
        key         TEXT PRIMARY KEY,
        filename    TEXT        NOT NULL,
        content     BYTEA       NOT NULL,
        size_bytes  BIGINT      NOT NULL,
        sha256      TEXT        NOT NULL,
        uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Одна и та же штатка, загруженная повторно, не дублируется (sha256 исходника).
    """
    CREATE TABLE IF NOT EXISTS raw_staffing_uploads (
        id          BIGSERIAL PRIMARY KEY,
        filename    TEXT        NOT NULL,
        content     BYTEA       NOT NULL,
        encrypted   BOOLEAN     NOT NULL,
        size_bytes  BIGINT      NOT NULL,
        sha256      TEXT        NOT NULL UNIQUE,
        uploaded_by TEXT,
        uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def enabled() -> bool:
    return bool(DSN)


def _conn():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ConnectionPool(DSN, min_size=1, max_size=POOL_MAX,
                                       kwargs={"row_factory": dict_row}, open=True)
    return _pool.connection()


def configure(dsn: str):
    """Переключить raw-БД (тесты). Пустая строка — выключить."""
    global _pool, DSN
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None
        DSN = dsn


def init_schema():
    if not enabled():
        print("[rawdb] NEIROMASTER_RAW_DB_DSN не задан — исходники хранятся только в data/documents/")
        return
    with _conn() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)


# ---------- Документы ----------
def put_document(key: str, filename: str, content: bytes):
    """Сохранить оригинал (повторная загрузка того же ключа заменяет его). No-op без raw-БД."""
    if not enabled():
        return
    with _conn() as conn:
        conn.execute(
            "INSERT INTO raw_documents (key, filename, content, size_bytes, sha256) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET "
            "filename = EXCLUDED.filename, content = EXCLUDED.content, "
            "size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256, uploaded_at = now()",
            (key, filename, content, len(content), hashlib.sha256(content).hexdigest()),
        )


def get_document(key: str) -> bytes | None:
    if not enabled():
        return None
    with _conn() as conn:
        row = conn.execute("SELECT content FROM raw_documents WHERE key = %s", (key,)).fetchone()
    return bytes(row["content"]) if row else None


def delete_document(key: str):
    if not enabled():
        return
    with _conn() as conn:
        conn.execute("DELETE FROM raw_documents WHERE key = %s", (key,))


# ---------- Штатные расписания ----------
def put_staffing(filename: str, content: bytes, uploaded_by: str | None = None):
    """Сохранить исходник загруженной штатки (зашифрованным, если шифрование ПДн включено)."""
    if not enabled():
        return
    import users
    f = users._fernet()
    stored = f.encrypt(content) if f else content
    with _conn() as conn:
        conn.execute(
            "INSERT INTO raw_staffing_uploads (filename, content, encrypted, size_bytes, sha256, uploaded_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
            (filename or "", stored, f is not None, len(content),
             hashlib.sha256(content).hexdigest(), uploaded_by),
        )


def get_staffing(upload_id: int) -> bytes | None:
    """Исходник штатки (расшифрованный) — для переобработки."""
    if not enabled():
        return None
    with _conn() as conn:
        row = conn.execute("SELECT content, encrypted FROM raw_staffing_uploads WHERE id = %s",
                           (upload_id,)).fetchone()
    if row is None:
        return None
    content = bytes(row["content"])
    if not row["encrypted"]:
        return content
    import users
    f = users._fernet()
    if f is None:
        raise RuntimeError("Штатка зашифрована, а ключ ПДн не задан (NEIROMASTER_PII_KEY=off?)")
    return f.decrypt(content)


# ---------- Перенос уже загруженного ----------
def backfill() -> int:
    """Документы из реестра, которых ещё нет в raw-БД, — залить туда с диска (или из S3).
    Идемпотентно: уже перенесённые пропускаются."""
    import config
    import docregistry
    import storage
    if not enabled():
        raise SystemExit("Задайте NEIROMASTER_RAW_DB_DSN — переносить некуда.")
    init_schema()
    with _conn() as conn:
        have = {r["key"] for r in conn.execute("SELECT key FROM raw_documents").fetchall()}
    moved = 0
    for entry in docregistry.list_documents():
        rel = entry.get("path") or entry.get("filename")
        key = entry.get("s3_key") or storage._key(rel)
        if not rel or key in have:
            continue
        path = config.DOCS_DIR / rel
        if not path.exists() and not storage.pull(path, key):
            print(f"[rawdb] нет ни локальной копии, ни копии в S3: {rel}")
            continue
        put_document(key, path.name, path.read_bytes())
        moved += 1
        print(f"[rawdb] перенесён: {rel}")
    return moved


if __name__ == "__main__":
    import sys
    import config  # noqa: F401 — грузит .env до чтения DSN
    configure(os.environ.get("NEIROMASTER_RAW_DB_DSN", "").strip())
    if "--backfill" in sys.argv:
        print(f"[rawdb] перенесено документов: {backfill()}")
    else:
        print(__doc__)
