"""
Диагностика очередей (RQ/Redis) — для страницы /queue-test. Только суперадмин.

Показывает живость цепочки web -> Redis -> worker: длину очереди, воркеров и их
состояние, число задач в работе/провалов, загрузку лимитера DeepSeek. Умеет ставить
безвредную тест-задачу (run_selftest) и следить за её выполнением.
"""
from fastapi import APIRouter, HTTPException

from deps import globaltest_only, owner_only
from redis_conn import get_redis_raw
import jobs
import ratelimit

router = APIRouter(prefix="/api/queue", dependencies=[*owner_only, *globaltest_only])   # тест очередей — за /globaltest


def _queue_or_400():
    raw = get_redis_raw()
    if raw is None:
        raise HTTPException(status_code=400,
                            detail="Redis не настроен (REDIS_URL пуст) — очередь работает в режиме потоков.")
    from rq import Queue
    return raw, Queue(jobs.QUEUE_NAME, connection=raw)


@router.get("/status")
def status():
    raw = get_redis_raw()
    if raw is None:
        return {"redis": False, "queue": jobs.QUEUE_NAME,
                "deepseek_in_flight": ratelimit.in_flight(),
                "deepseek_max": ratelimit.MAX_CONCURRENCY}
    from rq import Queue, Worker
    from rq.registry import StartedJobRegistry
    q = Queue(jobs.QUEUE_NAME, connection=raw)
    workers = []
    for w in Worker.all(connection=raw):
        try:
            workers.append({"name": w.name, "state": w.get_state(),
                            "current_job": w.get_current_job_id(),
                            "successful": getattr(w, "successful_job_count", None),
                            "failed": getattr(w, "failed_job_count", None)})
        except Exception:
            pass
    return {
        "redis": True,
        "queue": jobs.QUEUE_NAME,
        "queued": q.count,
        "started": StartedJobRegistry(queue=q).count,
        "failed": q.failed_job_registry.count,
        "finished": q.finished_job_registry.count,
        "workers": workers,
        "worker_count": len(workers),
        "deepseek_in_flight": ratelimit.in_flight(),
        "deepseek_max": ratelimit.MAX_CONCURRENCY,
    }


@router.post("/selftest")
def selftest(seconds: float = 2.0):
    raw, q = _queue_or_400()
    job = q.enqueue(jobs.run_selftest, float(seconds), "selftest",
                    job_timeout=60, result_ttl=300, failure_ttl=300)
    return {"job_id": job.id, "enqueued_at": str(job.enqueued_at or "")}


@router.get("/job/{job_id}")
def job_status(job_id: str):
    raw, _ = _queue_or_400()
    from rq.job import Job
    try:
        job = Job.fetch(job_id, connection=raw)
    except Exception:
        raise HTTPException(status_code=404, detail="Задача не найдена (или истёк срок хранения результата).")
    st = job.get_status()
    return {
        "job_id": job.id,
        "status": st,
        "result": job.result,
        "enqueued_at": str(job.enqueued_at or ""),
        "started_at": str(job.started_at or ""),
        "ended_at": str(job.ended_at or ""),
        "exc": (job.exc_info or "")[:800] if st == "failed" else None,
    }
