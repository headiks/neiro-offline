"""
Ключ шифрования ПДн (pii_key.py): создаётся сам, один на все воркеры, не подменяется
молча. БД здесь не нужна — отпечаток в app_settings подменён словарём.
"""
import os
import stat
import sys
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import pii_key

BASE = Path(__file__).resolve().parents[1] / "backend"   # модули приложения


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = {}
    monkeypatch.setenv("NEIROMASTER_PII_KEY_FILE", str(tmp_path / "secrets" / "pii.key"))
    monkeypatch.delenv("NEIROMASTER_PII_KEY", raising=False)
    monkeypatch.setattr(pii_key, "_stored_fingerprint", lambda: settings.get("fp"))
    monkeypatch.setattr(pii_key, "_store_fingerprint", lambda fp: settings.setdefault("fp", fp))
    yield {"file": tmp_path / "secrets" / "pii.key", "settings": settings}
    os.environ.pop("NEIROMASTER_PII_KEY", None)


def test_generated_on_first_start(env):
    key = pii_key.ensure()
    Fernet(key.encode())                                    # настоящий ключ Fernet
    assert env["file"].read_text().strip() == key
    assert stat.S_IMODE(env["file"].stat().st_mode) == 0o600
    assert os.environ["NEIROMASTER_PII_KEY"] == key         # все модули читают одно значение
    assert env["settings"]["fp"] == pii_key.fingerprint(key)
    assert key not in env["settings"]["fp"]                 # в БД — только отпечаток


def test_file_reused_after_restart(env):
    key = pii_key.ensure()
    os.environ.pop("NEIROMASTER_PII_KEY")                   # «перезапуск» процесса
    assert pii_key.ensure() == key


def test_env_key_wins_and_is_not_written(env):
    key = Fernet.generate_key().decode()
    os.environ["NEIROMASTER_PII_KEY"] = key
    assert pii_key.ensure() == key
    assert not env["file"].exists()


def test_other_key_refused(env):
    pii_key.ensure()
    os.environ["NEIROMASTER_PII_KEY"] = Fernet.generate_key().decode()
    with pytest.raises(pii_key.KeyMismatch):
        pii_key.ensure()


def test_lost_key_file_is_not_silently_replaced(env):
    pii_key.ensure()
    os.environ.pop("NEIROMASTER_PII_KEY")
    env["file"].unlink()
    with pytest.raises(pii_key.KeyMismatch):
        pii_key.ensure()                                    # данные зашифрованы, ключа нет
    assert not env["file"].exists()


def test_broken_file_and_bad_env(env):
    env["file"].parent.mkdir(parents=True)
    env["file"].write_text("not-a-key")
    with pytest.raises(pii_key.KeyMismatch):
        pii_key.ensure()
    os.environ["NEIROMASTER_PII_KEY"] = "short"
    with pytest.raises(pii_key.KeyMismatch):
        pii_key.ensure()


def test_off_disables(env):
    os.environ["NEIROMASTER_PII_KEY"] = "off"
    assert pii_key.ensure() == "" and pii_key.current() == ""
    assert not env["file"].exists()


def test_parallel_workers_get_one_key(tmp_path):
    """gunicorn поднимает воркеры одновременно — ключ должен получиться один."""
    path = tmp_path / "pii.key"
    code = "import pii_key; print(pii_key.ensure(check_db=False))"
    env = {**os.environ, "NEIROMASTER_PII_KEY_FILE": str(path), "NEIROMASTER_PII_KEY": ""}
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=BASE, env=env,
                              stdout=subprocess.PIPE, text=True) for _ in range(6)]
    keys = {p.communicate(timeout=60)[0].strip().splitlines()[-1] for p in procs}
    assert len(keys) == 1 and keys == {path.read_text().strip()}
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_users_encrypt_with_auto_key(env):
    import users
    ct = users._encrypt_field("Иванов Иван")
    assert ct.startswith("enc:") and env["file"].exists()
    assert users._decrypt_field(ct) == "Иванов Иван"
    assert users._encrypt_field(ct) == ct                   # повторно не шифруется
