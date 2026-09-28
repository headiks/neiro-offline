"""
Постановка тяжёлых задач в очередь (Redis Queue / RQ) с откатом на потоки.

Тяжёлую работу (разбор PDF + LLM-классификация, генерация плана, реанализ базы) в
высоконагруженном режиме НЕ делаем внутри web-воркера: он должен только принять
запрос, поставить задачу и отдать job_id. Саму работу выполняют отдельные
worker-процессы (`python worker.py` → RQ worker), которых можно масштабировать
числом независимо от web-воркеров. Прогресс/отмена — через jobstore (Redis), виден
всем процессам.

Есть Redis — enqueue_* кладут задачу в RQ. Нет Redis — выполняем в daemon-потоке
(прежнее одно-процессное поведение). Функции-задачи (run_*) импортируют тяжёлые
модули лениво, внутри тела, чтобы не ловить циклические импорты.
"""
import os
import threading

from redis_conn import get_redis, get_redis_raw

QUEUE_NAME = os.environ.get("NEIROMASTER_QUEUE", "nm")
# Потолок времени на одну задачу. Реанализ всей базы длиннее — свой таймаут ниже.
JOB_TIMEOUT = int(os.environ.get("NEIROMASTER_JOB_TIMEOUT", "3600"))
REANALYZE_ALL_TIMEOUT = int(os.environ.get("NEIROMASTER_REANALYZE_TIMEOUT", str(6 * 3600)))


def _queue():
    r = get_redis_raw()   # RQ хранит pickled-данные — нужен клиент без decode_responses
    if r is None:
        return None
    from rq import Queue
    return Queue(QUEUE_NAME, connection=r)


def _run_or_thread(q, target, *args, job_timeout=JOB_TIMEOUT):
    """Redis есть — в RQ; нет — в поток. Возврат: True, если ушло в очередь."""
    if q is not None:
        q.enqueue(target, *args, job_timeout=job_timeout,
                  result_ttl=600, failure_ttl=24 * 3600)
        return True
    threading.Thread(target=target, args=args, daemon=True).start()
    return False


# ---------- Задачи (выполняются в worker-процессе или в потоке-фолбэке) ----------
def run_ingest(filepath: str, filename: str, job_id: str, force: bool):
    import docpipe.pipeline as pl
    pl.ingest(filepath, filename=filename, job_id=job_id, force=force,
              progress_cb=None)


def run_index(job_id: str):
    import indexing
    indexing.process_index_job(job_id)


def run_reanalyze_all(job_id: str):
    import indexing
    indexing.reanalyze_all(job_id)


def run_reanalyze_document(filename: str):
    import indexing
    indexing.reanalyze_document(filename)
    indexing.docs_changed()


def run_generation(job_id: str, plan: dict, profs: list, only_missing: bool):
    import planner
    planner._run_generation(job_id, plan, profs, only_missing)


def run_selftest(seconds: float = 2.0, tag: str = ""):
    """Безвредная тест-задача для страницы проверки очередей: подождать и вернуть,
    какой worker её выполнил. Не трогает БД/DeepSeek — только показывает, что цепочка
    web -> Redis -> worker жива."""
    import os
    import time as _t
    _t.sleep(max(0.0, min(float(seconds), 30.0)))
    return {"ok": True, "tag": tag, "worker_pid": os.getpid(),
            "finished_at": _t.strftime("%Y-%m-%d %H:%M:%S")}


# ---------- Постановка в очередь (зовётся из web-воркера) ----------
def enqueue_ingest(filepath: str, filename: str, job_id: str, force: bool = False) -> bool:
    return _run_or_thread(_queue(), run_ingest, filepath, filename, job_id, force)


def enqueue_index(job_id: str) -> bool:
    return _run_or_thread(_queue(), run_index, job_id)


def enqueue_reanalyze_all(job_id: str) -> bool:
    return _run_or_thread(_queue(), run_reanalyze_all, job_id,
                          job_timeout=REANALYZE_ALL_TIMEOUT)


def enqueue_reanalyze_document(filename: str) -> bool:
    return _run_or_thread(_queue(), run_reanalyze_document, filename)


def enqueue_generation(job_id: str, plan: dict, profs: list, only_missing: bool) -> bool:
    return _run_or_thread(_queue(), run_generation, job_id, plan, profs, only_missing)
