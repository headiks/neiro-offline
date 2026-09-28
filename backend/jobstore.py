"""
Разделяемое хранилище состояния фоновых задач (прогресс, статус, флаг отмены).

Зачем: прогресс генерации плана и индексации раньше жил в in-memory dict одного
процесса. При нескольких web-воркерах и отдельных worker-процессах это ломается —
воркер, обслуживающий опрос прогресса, не видит задачу, запущенную в другом
процессе, а «Отмена» из web-воркера не доходит до worker'а, гоняющего LLM.

Здесь один тонкий интерфейс: set/get по (namespace, job_id). Есть Redis — состояние
в Redis-хэше (виден всем процессам, TTL сам чистит старьё). Нет Redis — обычный
словарь в памяти (прежнее одно-процессное поведение). namespace разделяет виды
задач: "gen" (генерация плана), "index" (индексация/реанализ).
"""
import json
import threading
from typing import Optional

from redis_conn import get_redis

_TTL = 24 * 3600  # сутки — задачи короче; TTL страхует от утечки ключей

# Фолбэк-хранилище в памяти (когда Redis нет).
_mem: dict = {}
_mem_lock = threading.Lock()


def _key(ns: str, job_id: str) -> str:
    return f"nmjob:{ns}:{job_id}"


def set_job(ns: str, job_id: str, /, **fields) -> dict:
    """Создаёт/обновляет задачу (слияние полей). Возвращает полный текущий снимок.
    ns и job_id — только позиционно: поле с именем job_id (замок генерации плана хранит
    в нём id задачи) уходит в fields, а не конфликтует с ключом (раньше — TypeError)."""
    r = get_redis()
    if r is None:
        with _mem_lock:
            job = _mem.setdefault(_key(ns, job_id), {"job_id": job_id})
            job.update(fields)
            return dict(job)
    k = _key(ns, job_id)
    # Пишем каждое поле как JSON-значение хэша — читаемо и типобезопасно.
    mapping = {kk: json.dumps(vv, ensure_ascii=False) for kk, vv in fields.items()}
    mapping.setdefault("job_id", json.dumps(job_id))
    r.hset(k, mapping=mapping)
    r.expire(k, _TTL)
    return get_job(ns, job_id) or {"job_id": job_id, **fields}


def get_job(ns: str, job_id: str) -> Optional[dict]:
    r = get_redis()
    if r is None:
        with _mem_lock:
            job = _mem.get(_key(ns, job_id))
            return dict(job) if job else None
    raw = r.hgetall(_key(ns, job_id))
    if not raw:
        return None
    out = {}
    for kk, vv in raw.items():
        try:
            out[kk] = json.loads(vv)
        except Exception:
            out[kk] = vv
    return out


def cancel_job(ns: str, job_id: str) -> Optional[dict]:
    """Помечает задачу на отмену (если она ещё идёт). Рабочий цикл увидит флаг."""
    job = get_job(ns, job_id)
    if not job:
        return None
    if job.get("status") in ("queued", "running", "processing"):
        return set_job(ns, job_id, cancel=True)
    return job


# ---------- Короткие замки (одна операция на ключ) ----------
_claims: dict = {}


def claim(ns: str, key: str, ttl: int = 300) -> bool:
    """Атомарно занять ключ на ttl секунд. False — уже занят (другой процесс/запрос).
    Redis — SET NX (общий для всех воркеров), без Redis — словарь процесса."""
    import time
    k = f"nmclaim:{ns}:{key}"
    r = get_redis()
    if r is not None:
        try:
            return bool(r.set(k, "1", nx=True, ex=ttl))
        except Exception:
            pass
    now = time.time()
    with _mem_lock:
        if _claims.get(k, 0) > now:
            return False
        _claims[k] = now + ttl
        return True


def release(ns: str, key: str):
    k = f"nmclaim:{ns}:{key}"
    r = get_redis()
    if r is not None:
        try:
            r.delete(k)
        except Exception:
            pass
    with _mem_lock:
        _claims.pop(k, None)
