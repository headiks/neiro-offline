"""
Ключ шифрования ПДн в БД (Fernet) — создаётся автоматически.

Откуда берётся ключ (по порядку):
  1. переменная окружения NEIROMASTER_PII_KEY (например, из .env.production);
  2. файл ключа (NEIROMASTER_PII_KEY_FILE, по умолчанию data/secrets/pii.key);
  3. нет ни того, ни другого — ключ генерируется и записывается в файл (права 600).

Файл лежит на диске приложения, а не в БД: утёкший дамп или бэкап базы без файла
ключа не расшифровать. Поэтому файл ключа нужно бэкапить ОТДЕЛЬНО от базы — без него
зашифрованные ФИО и контакты не восстановить.

Защита от потери и подмены: в БД хранится отпечаток ключа (SHA-256, первые 16 знаков,
сам ключ по нему не восстановить). Если ключ на старте не совпал с отпечатком — данные
зашифрованы другим ключом, и приложение не стартует с понятной ошибкой: иначе новые
записи шифровались бы новым ключом, а старые перестали бы читаться.

Выключить шифрование (только разработка и тесты): NEIROMASTER_PII_KEY=off.
"""
import hashlib
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]   # корень проекта (код — в backend/)
DEFAULT_KEY_FILE = BASE_DIR / "data" / "secrets" / "pii.key"
_FP_SETTING = "pii_key_fingerprint"
_DISABLED = ("off", "0", "false", "no", "disabled")
_lock = threading.Lock()


class KeyMismatch(RuntimeError):
    """Ключ не совпадает с тем, которым зашифрованы данные в БД."""


def key_file() -> Path:
    return Path(os.environ.get("NEIROMASTER_PII_KEY_FILE") or DEFAULT_KEY_FILE)


def disabled() -> bool:
    return (os.environ.get("NEIROMASTER_PII_KEY") or "").strip().lower() in _DISABLED


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _valid(key: str) -> bool:
    try:
        from cryptography.fernet import Fernet
        Fernet(key.encode("ascii"))
        return True
    except Exception:
        return False


def _read_file(path: Path) -> str:
    key = path.read_text(encoding="ascii").strip()
    if not _valid(key):
        raise KeyMismatch(f"Файл ключа ПДн повреждён: {path}")
    return key


def _create_file(path: Path) -> str:
    """Создать файл с новым ключом. Несколько воркеров стартуют одновременно: создание
    атомарное (O_EXCL), проигравший читает ключ победителя — ключ получается один."""
    from cryptography.fernet import Fernet
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    key = Fernet.generate_key().decode("ascii")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key + "\n")
    try:
        os.link(tmp, path)                 # атомарно и без перезаписи чужого файла
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        return _read_file(path)
    tmp.unlink(missing_ok=True)
    print("=" * 70)
    print(f"Создан ключ шифрования персональных данных: {path}")
    print("  Сохраните копию этого файла ОТДЕЛЬНО от бэкапов БД: без него")
    print("  зашифрованные ФИО и контакты сотрудников не восстановить.")
    print("=" * 70)
    return key


def _stored_fingerprint():
    try:
        import db
        row = db.query("SELECT value FROM app_settings WHERE key = %s", (_FP_SETTING,), "one")
        return (row or {}).get("value")
    except Exception:
        return None                        # таблицы ещё нет или БД недоступна


def _store_fingerprint(fp: str):
    import db
    db.execute("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
               (_FP_SETTING, fp))


def ensure(check_db: bool = True) -> str:
    """Ключ ПДн: из окружения, из файла или новый. Кладёт его в os.environ, чтобы все
    модули (users, questions) читали одно значение. '' — шифрование выключено явно."""
    with _lock:
        if disabled():
            return ""
        key = (os.environ.get("NEIROMASTER_PII_KEY") or "").strip()
        stored = _stored_fingerprint() if check_db else None
        if not key:
            path = key_file()
            if path.exists():
                key = _read_file(path)
            elif stored:
                # Данные уже зашифрованы, а ключа нет: новый ключ сделал бы их нечитаемыми.
                raise KeyMismatch(
                    "Ключ шифрования ПДн не найден, а данные в БД им уже зашифрованы. "
                    f"Верните файл ключа ({path}) или задайте NEIROMASTER_PII_KEY.")
            else:
                key = _create_file(path)
            os.environ["NEIROMASTER_PII_KEY"] = key
        if not _valid(key):
            raise KeyMismatch("NEIROMASTER_PII_KEY — не ключ Fernet (32 байта в base64).")
        if check_db:
            fp = fingerprint(key)
            if stored and stored != fp:
                raise KeyMismatch(
                    "Ключ шифрования ПДн не совпадает с тем, которым зашифрованы данные в БД "
                    f"(отпечаток в БД {stored}, у ключа {fp}). Верните прежний ключ.")
            if not stored:
                try:
                    _store_fingerprint(fp)
                except Exception:
                    pass                   # таблицы ещё нет — отпечаток запишется при старте
        return key


def current() -> str:
    """Ключ для шифрования здесь и сейчас (без проверки БД — её делает старт приложения)."""
    if disabled():
        return ""
    key = (os.environ.get("NEIROMASTER_PII_KEY") or "").strip()
    return key or ensure(check_db=False)
