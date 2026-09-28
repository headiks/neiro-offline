"""
Очередь вопросов, на которые ассистент не смог ответить сам.

Сюда попадает вопрос сотрудника, когда:
  - маршрут «escalate» (ЧС/травма/конфликт — нужен человек), либо
  - маршрут «rag», но ответа нет: в базе не нашлось размеченных фрагментов.

Детектирует эти случаи rag.handle_question (route + risk_flag), а маршрутизацию
человеку выполняет /ask (api_chat.py): вопрос без ответа не теряется, а встаёт в очередь
администратору. Тот отвечает (обсудив со специалистом при необходимости), и ответ
сохраняется — сотрудник видит его в личном кабинете.

Хранилище — таблица questions в PostgreSQL (индексы под очередь и «мои вопросы»).
Текст вопроса, ответ и контакты сотрудника шифруются так же, как ПДн пользователей
(users._encrypt_field, ключ — pii_key.py). Прежний
data/pending_questions.json переносится в БД один раз при старте (migrate_from_file).
"""

import json
import time
import uuid
from pathlib import Path
from typing import Optional

import db
import users

BASE_DIR = Path(__file__).resolve().parents[1]   # корень проекта (код — в backend/)
DATA_DIR = BASE_DIR / "data"
QUESTIONS_PATH = DATA_DIR / "pending_questions.json"

REASON_ESCALATE = "escalate"    # ЧС / риск — приоритетный разбор
REASON_NO_ANSWER = "no_answer"  # в регламентах ответа не нашлось

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

ANSWER_MAX = 8000

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS questions (
    id                TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    user_id           TEXT,
    user_name         TEXT NOT NULL DEFAULT '',
    position          TEXT NOT NULL DEFAULT '',
    department        TEXT NOT NULL DEFAULT '',
    contact           TEXT NOT NULL DEFAULT '',
    mentor            TEXT NOT NULL DEFAULT '',
    question          TEXT NOT NULL,
    resolved_question TEXT,
    reason            TEXT NOT NULL,
    risk_type         TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    answer            TEXT,
    answered_by       TEXT,
    answered_at       TEXT
)
"""
CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_questions_status ON questions(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_questions_user ON questions(user_id, created_at)",
)
_COLUMNS = ("id", "created_at", "user_id", "user_name", "position", "department", "contact",
            "mentor", "question", "resolved_question", "reason", "risk_type", "status",
            "answer", "answered_by", "answered_at")
_ENCRYPTED = ("user_name", "contact", "mentor", "question", "resolved_question", "answer")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _row(r) -> dict:
    entry = {c: r[c] for c in _COLUMNS}
    for c in _ENCRYPTED:
        entry[c] = users._decrypt_field(entry[c])
    return entry


def _insert(entry: dict):
    values = [users._encrypt_field(entry.get(c)) if c in _ENCRYPTED else entry.get(c) for c in _COLUMNS]
    db.execute(f"INSERT INTO questions ({', '.join(_COLUMNS)}) VALUES ({', '.join('%s' for _ in _COLUMNS)}) "
               "ON CONFLICT (id) DO NOTHING", values)


def record(user: dict, question: str, resolved_question: Optional[str],
           reason: str, risk_type: Optional[str] = None) -> dict:
    """Ставит вопрос сотрудника в очередь администратору. Возвращает созданную запись."""
    user = user or {}
    entry = {
        "id": str(uuid.uuid4()),
        "created_at": _now(),
        "user_id": user.get("id"),
        "user_name": user.get("full_name") or user.get("username") or "Сотрудник",
        "position": user.get("position") or "",
        "department": user.get("department") or "",
        "contact": user.get("contact") or "",
        "mentor": user.get("mentor") or "",
        "question": question,
        "resolved_question": resolved_question or None,
        "reason": reason if reason in (REASON_ESCALATE, REASON_NO_ANSWER) else REASON_NO_ANSWER,
        "risk_type": risk_type or None,
        "status": STATUS_OPEN,
        "answer": None,
        "answered_by": None,
        "answered_at": None,
    }
    _insert(entry)
    return dict(entry)


def list_all(status: Optional[str] = None, limit: int = 1000) -> list:
    """Очередь для админа: открытые вперёд, ЧС в самый верх, дальше старые раньше новых."""
    where, params = "", []
    if status:
        where, params = "WHERE status = %s", [status]
    rows = db.query(
        f"SELECT * FROM questions {where} ORDER BY (status <> 'open'), (reason <> 'escalate'), "
        "created_at LIMIT %s", tuple(params + [max(1, min(int(limit), 5000))])) or []
    return [_row(r) for r in rows]


def list_for_user(user_id: str) -> list:
    """Вопросы сотрудника в порядке диалога: старые сверху, новые снизу."""
    rows = db.query("SELECT * FROM questions WHERE user_id = %s ORDER BY created_at LIMIT 500",
                    (user_id,)) or []
    return [_row(r) for r in rows]


def get(qid: str) -> Optional[dict]:
    r = db.query("SELECT * FROM questions WHERE id = %s", (qid,), "one")
    return _row(r) if r else None


def resolve(qid: str, answer: str, admin_name: str) -> Optional[dict]:
    answer = (answer or "").strip()[:ANSWER_MAX]
    if not answer:
        raise ValueError("Ответ не может быть пустым")
    row = db.query(
        "UPDATE questions SET answer = %s, answered_by = %s, answered_at = %s, status = %s "
        "WHERE id = %s RETURNING *",
        (users._encrypt_field(answer), admin_name or "Администратор", _now(), STATUS_RESOLVED, qid), "one")
    return _row(row) if row else None


def count_open() -> int:
    r = db.query("SELECT count(*) AS n FROM questions WHERE status = %s", (STATUS_OPEN,), "one")
    return r["n"] if r else 0


def init():
    db.execute(CREATE_TABLE)
    for stmt in CREATE_INDEXES:
        db.execute(stmt)


def encrypt_plaintext() -> int:
    """Открытые ПДн в вопросах -> зашифрованные (разово после появления ключа)."""
    return users.encrypt_plaintext_rows("questions", _ENCRYPTED)


def migrate_from_file(path: Optional[Path] = None) -> int:
    """Разовый перенос data/pending_questions.json в таблицу; файл -> .migrated."""
    path = path or QUESTIONS_PATH
    if not path.exists():
        return 0
    try:
        items = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return 0
    moved = 0
    for entry in items.values():
        if isinstance(entry, dict) and entry.get("id") and entry.get("question"):
            before = get(entry["id"])
            if before is None:
                _insert({**{c: None for c in _COLUMNS},
                         **{k: v for k, v in entry.items() if k in _COLUMNS},
                         "created_at": entry.get("created_at") or _now(),
                         "reason": entry.get("reason") or REASON_NO_ANSWER,
                         "status": entry.get("status") or STATUS_OPEN,
                         **{c: entry.get(c) or "" for c in ("user_name", "position", "department",
                                                             "contact", "mentor")}})
                moved += 1
    path.replace(path.with_suffix(".json.migrated"))
    return moved
