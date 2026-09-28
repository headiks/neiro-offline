"""
sizing.py — сколько процессов запускать, исходя из железа: ядра и память, которые видит
контейнер (cgroup-лимиты, иначе весь сервер). Явная переменная окружения главнее расчёта.

    web-процессы (gunicorn)     WEB_CONCURRENCY        — по ядрам, не больше 25% памяти
    RQ-воркеры (docling)        NEIROMASTER_RQ_WORKERS — половина ядер, по памяти
    пул соединений к БД         NEIROMASTER_DB_POOL    — делим max_connections Postgres

Память на процесс — по замерам: web ~0,35 ГБ; воркер с docling на больших PDF до 4–5 ГБ
пиково (на старой установке ловили OOM), в среднем ~2 ГБ; Postgres ×2, Redis,
Caddy и embed — около 1,5 ГБ. Сетевые потоки (DeepSeek, FCM) от железа не зависят —
их не считаем. Итог печатается при старте web и worker.

    python backend/sizing.py        # что получится на этом сервере
"""
import os

WEB_GB = 0.35
WORKER_GB = 2.0
BASE_GB = 1.5
PG_MAX_CONNECTIONS = 300         # command: max_connections в docker-compose.yml


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def cpus() -> int:
    n = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    quota = _read("/sys/fs/cgroup/cpu.max").split()           # «200000 100000» или «max 100000»
    if len(quota) == 2 and quota[0].isdigit():
        n = min(n, max(1, int(quota[0]) // int(quota[1])))
    return max(1, n)


def mem_gb() -> float:
    limit = _read("/sys/fs/cgroup/memory.max")
    total = 0
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) * 1024
    if limit.isdigit():
        total = min(total, int(limit)) if total else int(limit)
    return total / 2 ** 30 if total else 4.0                  # не узнали — скромно, 4 ГБ


def _env(name: str):
    v = os.environ.get(name, "").strip()
    return int(v) if v.isdigit() and int(v) > 0 else None


def web_workers(cpu: int = None, mem: float = None) -> int:
    cpu, mem = cpu or cpus(), mem or mem_gb()
    return _env("WEB_CONCURRENCY") or max(2, min(cpu, int(mem * 0.25 / WEB_GB)))


def rq_workers(cpu: int = None, mem: float = None) -> int:
    cpu, mem = cpu or cpus(), mem or mem_gb()
    spare = mem - BASE_GB - web_workers(cpu, mem) * WEB_GB
    return _env("NEIROMASTER_RQ_WORKERS") or max(1, min(max(1, cpu // 2), int(spare / WORKER_GB)))


def db_pool(cpu: int = None, mem: float = None) -> int:
    procs = web_workers(cpu, mem) + rq_workers(cpu, mem)
    return _env("NEIROMASTER_DB_POOL") or max(5, min(20, int(PG_MAX_CONNECTIONS * 0.8) // procs))


def push_workers(cpu: int = None) -> int:
    """Потоки отправки пушей: почти всё время ждут ответа FCM (~0,3 с), CPU тратят мало —
    по 12 на ядро, от 10 до 100 (память на поток — единицы МБ)."""
    return _env("NEIROMASTER_PUSH_WORKERS") or max(10, min(100, (cpu or cpus()) * 12))


def summary() -> str:
    return (f"{cpus()} ядер, {mem_gb():.1f} ГБ -> web-процессов {web_workers()}, "
            f"RQ-воркеров {rq_workers()}, пул БД {db_pool()} на процесс, потоков пушей {push_workers()}")


if __name__ == "__main__":
    print(summary())
