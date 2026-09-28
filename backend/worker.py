"""
Worker-процесс для тяжёлых задач (разбор+классификация документов, генерация
планов, реанализ базы). Запускается отдельно от web:  `python worker.py`.

Столько worker-процессов, сколько нужно пропускной способности — они независимы
от web-воркеров. Все берут задачи из общей очереди Redis (RQ). Прогресс/отмена — в
общем jobstore (Redis), поэтому видны web-воркерам, рисующим прогресс-бары.

Требует Redis (REDIS_URL). Без него тяжёлую работу выполняют сами web-воркеры в
потоках (см. jobs.py) — тогда worker.py не нужен.
"""
import os

import config  # noqa: F401  — импорт грузит .env (ключ DeepSeek, DSN БД, REDIS_URL)
from redis_conn import get_redis, get_redis_raw
from jobs import QUEUE_NAME


def _requeue_once(r):
    """Возврат зависших задач в очередь — РОВНО один worker (общий замок с TTL),
    иначе каждый запущенный worker поставил бы дубли."""
    if not r.set("nm:lock:requeue", str(os.getpid()), nx=True, ex=120):
        return
    try:
        import indexing
        n1 = indexing.requeue_stranded()
    except Exception as e:
        print(f"[worker] requeue index: {e}"); n1 = 0
    try:
        import docpipe
        n2 = docpipe.requeue_stranded()
    except Exception as e:
        print(f"[worker] requeue docpipe: {e}"); n2 = 0
    print(f"[worker] возобновлено зависших задач: индексация={n1}, docpipe={n2}")


def main():
    r = get_redis()
    if r is None:
        raise SystemExit(
            "worker.py требует Redis. Задай REDIS_URL (напр. redis://localhost:6379/0) "
            "и запусти Redis, либо не запускай worker — тогда задачи выполняют web-воркеры."
        )
    # Схема БД и docpipe должны существовать до обработки задач.
    import db
    db.init_schema()
    import rawdb
    rawdb.init_schema()
    try:
        import docpipe
        docpipe.init_schema()
    except Exception as e:
        print(f"[worker] docpipe init: {e}")

    _requeue_once(r)

    from rq import Queue, SimpleWorker
    rq_conn = get_redis_raw()   # RQ хранит pickled-данные — клиент без decode_responses
    q = Queue(QUEUE_NAME, connection=rq_conn)
    print(f"[worker] старт RQ SimpleWorker на очереди '{QUEUE_NAME}' (pid {os.getpid()})")
    # SimpleWorker (без fork) намеренно: дефолтный форкающийся Worker наследовал бы в
    # дочернем процессе пул psycopg (сокеты БД не переживают fork -> порча соединений).
    # Одна задача на процесс; масштаб — числом процессов rag-worker@N, а внутри задачи
    # параллелят ThreadPoolExecutor'ы (NEIROMASTER_DOCPIPE_WORKERS / _GEN_WORKERS).
    SimpleWorker([q], connection=rq_conn).work()


if __name__ == "__main__":
    main()
