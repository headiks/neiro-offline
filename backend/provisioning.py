"""
provisioning.py — создание изолированного кабинета компании.

Модель мультитенантности: «схема на кабинет» (schema-per-tenant). Каждая компания
получает свою PostgreSQL-схему со своим полным набором таблиц — данные клиентов
физически изолированы в одной базе. Подходит для десятков-сотен кабинетов.

Kafka здесь не нужна: создание кабинета — редкая синхронная операция с немедленным
результатом («схема создалась / нет»), а не поток событий. Всё делается одним
рукописным скриптом поверх уже существующих init_schema (db, docpipe, documents).

    python provisioning.py <slug> [--company "ООО Ромашка"]   # создать кабинет
    python provisioning.py --list                              # список кабинетов

Что делает provision_cabinet():
    CREATE SCHEMA "cab_<slug>"
      -> все CREATE TABLE в этой схеме (db + docpipe + document_meta)
      -> посев стартовой структуры (этапы + папки) из knowledge_seed.json
      -> регистрация кабинета в реестре public.cabinets
      -> S3-префикс кабинета cab_<slug>/… (изоляция оригиналов; no-op без S3)
      -> Qdrant-коллекции кабинета cab_<slug>__{reglaments,docpipe,folder_tags,doc_summaries}
"""
import re
import sys
import time
import json

import db
import stages
import folders
import documents
import config
from config import BASE_DIR

SCHEMA_PREFIX = "cab_"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,40}$")

def schema_for(slug: str) -> str:
    """slug кабинета -> имя схемы. Валидирует slug (латиница/цифры/подчёркивание)."""
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("slug кабинета: латиница/цифры/подчёркивание, начинается с буквы или цифры")
    return f"{SCHEMA_PREFIX}{slug}"


def s3_prefix_for(slug: str) -> str:
    """Префикс кабинета в бакете: cab_<slug>/<глобальный S3_PREFIX>. Оригиналы разных
    компаний физически разложены по своим «папкам» одного бакета."""
    schema_for(slug)   # валидация slug
    return f"{SCHEMA_PREFIX}{slug.strip().lower()}/{config.S3_PREFIX}"


def ensure_registry():
    """Реестр кабинетов в public — общий для всех тенантов (какие кабинеты есть)."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS public.cabinets ("
        "  slug        TEXT PRIMARY KEY,"
        "  schema_name TEXT UNIQUE NOT NULL,"
        "  company     TEXT NOT NULL DEFAULT '',"
        "  created_at  TEXT NOT NULL)"
    )


def cabinet_exists(schema: str) -> bool:
    r = db.query(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,), fetch="one",
    )
    return r is not None


def list_cabinets() -> list:
    ensure_registry()
    return db.query("SELECT slug, schema_name, company, created_at FROM public.cabinets ORDER BY created_at")


def _provision_s3(slug: str) -> dict:
    """Поднять S3-префикс кабинета: проверить бакет и создать маркер cab_<slug>/…/.keep,
    чтобы префикс существовал в листингах. No-op при выключенном S3. Сбой не роняет
    создание кабинета — префикс появится при первой заливке оригинала."""
    prefix = s3_prefix_for(slug)
    if not config.S3_ENABLED:
        print(f"[s3] {slug}: S3 выключен — префикс {prefix!r} заведётся при включении S3")
        return {"enabled": False, "prefix": prefix, "status": "skipped"}
    try:
        import storage
        s3 = storage._s3()
        s3.head_bucket(Bucket=config.S3_BUCKET)                          # бакет доступен
        s3.put_object(Bucket=config.S3_BUCKET, Key=f"{prefix}.keep", Body=b"")   # «поднять» префикс
        print(f"[s3] {slug}: префикс {prefix!r} готов (bucket={config.S3_BUCKET})")
        return {"enabled": True, "prefix": prefix, "bucket": config.S3_BUCKET, "status": "created"}
    except Exception as e:
        print(f"[warn] S3-префикс для {slug}: {e}")
        return {"enabled": True, "prefix": prefix, "status": "error", "error": str(e)}


def provision_cabinet(slug: str, company: str = "", seed_path=None) -> str:
    """Создать кабинет: схема + все таблицы + посев + S3-префикс + запись в реестр.
    Возвращает имя схемы. Векторного стора (Qdrant) нет — ретрив идёт по LLM-меткам в Postgres."""
    schema = schema_for(slug)
    ensure_registry()
    if cabinet_exists(schema):
        raise ValueError(f"Кабинет уже существует: схема {schema!r}")

    # 1) сама схема
    with db._get_pool().connection() as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    # 2) все таблицы + посев — внутри схемы кабинета (search_path)
    with db.use_schema(schema):
        db.init_schema()                      # users, sessions, folders, stages, substages
        try:
            import docpipe
            docpipe.init_schema()             # конвейер разметки v2
        except Exception as e:
            print(f"[warn] docpipe schema в {schema}: {e}")
        documents.init()                      # document_meta (реестр v1)

        seed_path = seed_path or (BASE_DIR / "data" / "knowledge_seed.json")
        if seed_path.exists():
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            s = stages.seed_if_empty(seed.get("stages", []))
            f = folders.seed_if_empty(seed.get("folders", []))
            print(f"[seed] {schema}: этапов {s}, папок {f}")
        else:
            print(f"[warn] нет файла сида {seed_path} — кабинет создан без стартовой структуры")

    # 3) регистрация в общем реестре (public)
    db.execute(
        "INSERT INTO public.cabinets (slug, schema_name, company, created_at) VALUES (%s, %s, %s, %s)",
        (slug.strip().lower(), schema, company or "", time.strftime("%Y-%m-%dT%H:%M:%S")),
    )

    # 4) S3-префикс кабинета — изоляция оригиналов документов компании
    _provision_s3(slug)

    return schema


def _main(argv):
    if "--list" in argv:
        rows = list_cabinets()
        if not rows:
            print("Кабинетов пока нет.")
        for r in rows:
            print(f"  {r['slug']:<20} {r['schema_name']:<24} {r['company']}  ({r['created_at']})")
        return
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return
    slug = args[0]
    company = ""
    if "--company" in argv:
        i = argv.index("--company")
        company = argv[i + 1] if i + 1 < len(argv) else ""
    schema = provision_cabinet(slug, company=company)
    print(f"Кабинет создан: схема {schema}")


def _selfcheck():
    """Смоук-тест чистых хелперов имён (схема, S3-префикс) без сети/БД."""
    assert schema_for("acme") == "cab_acme"
    assert schema_for(" Acme ") == "cab_acme"
    for bad in ("", "1acme-x", "acme;drop", "переезд"):
        try:
            schema_for(bad)
            raise AssertionError(f"должно было упасть: {bad!r}")
        except ValueError:
            pass
    # S3-префикс кабинета: cab_<slug>/<глобальный префикс>
    config.S3_PREFIX = "documents/"
    assert s3_prefix_for("acme") == "cab_acme/documents/"
    assert s3_prefix_for(" Acme ") == "cab_acme/documents/"
    for bad in ("", "acme;drop"):
        try:
            s3_prefix_for(bad); raise AssertionError("s3_prefix_for должен валидировать slug")
        except ValueError:
            pass
    print("provisioning: schema_for / s3_prefix_for — OK")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _main(sys.argv[1:])
    else:
        _selfcheck()
