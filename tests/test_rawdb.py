"""
raw-БД исходников (rawdb) на настоящей PostgreSQL:
  - оригинал документа сохраняется, читается и удаляется через storage (единая точка);
  - потерянный локальный кэш восстанавливается из raw-БД;
  - штатка хранится зашифрованной ключом ПДн, повторная загрузка не дублируется.

Нужна тестовая PostgreSQL (NEIROMASTER_TEST_RAW_DSN, по умолчанию — NEIROMASTER_TEST_DSN);
нет БД — тесты пропускаются.
"""
import os
import tempfile
from pathlib import Path

import pytest

TEST_DSN = (os.environ.get("NEIROMASTER_TEST_RAW_DSN") or os.environ.get("NEIROMASTER_TEST_DSN")
            or "postgresql://neiromaster:neiromaster@localhost:5432/neiromaster_test")

os.environ.update({
    "NEIROMASTER_RAW_DB_DSN": TEST_DSN,
    "NEIROMASTER_S3_ENDPOINT": "",            # только raw-БД, без S3
    "NEIROMASTER_PII_KEY": "",
    "NEIROMASTER_PII_KEY_FILE": os.path.join(tempfile.mkdtemp(), "secrets", "pii.key"),
})

try:
    import psycopg
    with psycopg.connect(TEST_DSN, connect_timeout=3) as _c:
        _c.execute("DROP TABLE IF EXISTS raw_documents, raw_staffing_uploads")
    DB_OK = True
except Exception:                    # noqa: BLE001 — любая причина = нет БД
    DB_OK = False

pytestmark = pytest.mark.skipif(not DB_OK, reason="тестовая PostgreSQL недоступна")


@pytest.fixture(scope="module")
def raw():
    import rawdb
    rawdb.configure(TEST_DSN)
    rawdb.init_schema()
    yield rawdb
    rawdb.configure("")


def test_document_roundtrip_through_storage(raw, tmp_path):
    import storage
    storage.put("Регламент.pdf", b"%PDF-1.4 original", "documents/super/ivanov/Регламент.pdf")
    assert raw.get_document("documents/super/ivanov/Регламент.pdf") == b"%PDF-1.4 original"

    # Локальный кэш потерян — pull восстанавливает оригинал из raw-БД.
    local = tmp_path / "Регламент.pdf"
    assert storage.pull(local, "documents/super/ivanov/Регламент.pdf") is True
    assert local.read_bytes() == b"%PDF-1.4 original"

    # Повторная загрузка заменяет оригинал, удаление убирает его.
    storage.put("Регламент.pdf", b"v2", "documents/super/ivanov/Регламент.pdf")
    assert raw.get_document("documents/super/ivanov/Регламент.pdf") == b"v2"
    storage.delete("Регламент.pdf", "documents/super/ivanov/Регламент.pdf")
    assert raw.get_document("documents/super/ivanov/Регламент.pdf") is None
    assert storage.pull(tmp_path / "нет.pdf", "documents/нет.pdf") is False


def test_staffing_encrypted_and_deduplicated(raw):
    content = "ФИО;Должность\nИванов Иван Иванович;Токарь\n".encode("utf-8")
    raw.put_staffing("штатка.csv", content, "user-1")
    raw.put_staffing("штатка (копия).csv", content, "user-1")      # тот же файл повторно

    with psycopg.connect(TEST_DSN) as c:
        rows = c.execute("SELECT id, content, encrypted FROM raw_staffing_uploads").fetchall()
    assert len(rows) == 1, "повторная загрузка того же файла не должна дублироваться"
    upload_id, stored, encrypted = rows[0]
    assert encrypted and "Иванов".encode("utf-8") not in bytes(stored), "ФИО не должны лежать открыто"
    assert raw.get_staffing(upload_id) == content


def test_disabled_is_noop(tmp_path):
    import rawdb
    import storage
    rawdb.configure("")
    try:
        storage.put("a.pdf", b"x", "documents/a.pdf")          # без raw-БД и S3 — ничего не делает
        assert rawdb.get_document("documents/a.pdf") is None
        assert storage.pull(Path(tmp_path) / "a.pdf", "documents/a.pdf") is False
    finally:
        rawdb.configure(TEST_DSN)
