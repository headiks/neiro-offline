"""Хеш пароля раздела /globaltest для env NEIROMASTER_GLOBALTEST_PASSWORD_HASH.

    python scripts/globaltest_hash.py            # спросит пароль (не попадёт в историю shell)

Открытым текстом пароль нигде не хранится — только этот хеш (scrypt, как у пользователей).
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
os.environ.setdefault("NEIROMASTER_PII_KEY", "off")
import users  # noqa: E402

pw = getpass.getpass("Пароль раздела /globaltest: ")
if len(pw) < 8:
    sys.exit("Пароль — от 8 символов")
salt, digest = users.hash_password(pw)
print(f"NEIROMASTER_GLOBALTEST_PASSWORD_HASH={salt}:{digest}")
