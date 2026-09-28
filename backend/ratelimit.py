"""
Глобальный лимитер одновременных вызовов DeepSeek.

Главная защита от перегрузки при высокой нагрузке. При 10k пользователях узкое
место — не локальные потоки, а пропускная способность внешнего API DeepSeek: без
кап'а на суммарные одновременные запросы сотни воркеров устроят шторм 503 «Service
is too busy». Здесь распределённый семафор: сколько бы процессов/потоков ни
работало, одновременно «в полёте» не больше DEEPSEEK_MAX_CONCURRENCY вызовов;
остальные ждут своей очереди (до acquire_timeout, потом пропускаем — пусть обычный
ретрай DeepSeek разрулит).

Есть Redis — семафор общий на весь кластер (атомарный Lua-скрипт по ZSET; истёкшие
слоты сами возвращаются, если процесс умер в вызове). Нет Redis — локальный на
процесс (BoundedSemaphore), чего достаточно для одно-процессного dev.
"""
import os
import time
import uuid
import threading
from contextlib import contextmanager

from redis_conn import get_redis

MAX_CONCURRENCY = int(os.environ.get("DEEPSEEK_MAX_CONCURRENCY", "24"))
# Ждём слот не дольше этого; затем идём без слота (ретраи DeepSeek подстрахуют).
ACQUIRE_TIMEOUT = int(os.environ.get("DEEPSEEK_ACQUIRE_TIMEOUT", "120"))
# Слот живёт максимум столько — страховка от утечки, если держатель умер.
_SLOT_TTL = int(os.environ.get("DEEPSEEK_TIMEOUT", "300")) + 30

# Атомарно: убрать протухшие слоты, и если есть место — занять. Возврат 1/0.
_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - ttl)
if redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ttl)
  return 1
end
return 0
"""

_KEY = "nm:deepseek:slots"
_local_sem = threading.BoundedSemaphore(MAX_CONCURRENCY)


@contextmanager
def deepseek_slot(timeout: int = None):
    """Держит один слот на время вызова DeepSeek. Освобождает даже при исключении."""
    timeout = ACQUIRE_TIMEOUT if timeout is None else timeout
    r = get_redis()
    if r is None:
        got = _local_sem.acquire(timeout=timeout)
        try:
            yield
        finally:
            if got:
                _local_sem.release()
        return

    token = uuid.uuid4().hex
    ttl_ms = _SLOT_TTL * 1000
    acquired = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ok = r.eval(_ACQUIRE_LUA, 1, _KEY,
                        int(time.time() * 1000), MAX_CONCURRENCY, ttl_ms, token)
        except Exception:
            break  # Redis сдох посреди работы — не блокируем вызов
        if ok == 1:
            acquired = True
            break
        time.sleep(0.05)
    try:
        yield
    finally:
        if acquired:
            try:
                r.zrem(_KEY, token)
            except Exception:
                pass  # протухнет по TTL


def in_flight() -> int:
    """Сколько вызовов сейчас в полёте (для диагностики/метрик)."""
    r = get_redis()
    if r is None:
        return MAX_CONCURRENCY - _local_sem._value  # best-effort
    try:
        r.zremrangebyscore(_KEY, 0, int(time.time() * 1000) - _SLOT_TTL * 1000)
        return int(r.zcard(_KEY))
    except Exception:
        return -1


if __name__ == "__main__":
    # Мини-проверка: локальный семафор не пускает больше лимита одновременно.
    import concurrent.futures as cf
    peak = [0]
    cur = [0]
    lk = threading.Lock()

    def work():
        with deepseek_slot(timeout=5):
            with lk:
                cur[0] += 1
                peak[0] = max(peak[0], cur[0])
            time.sleep(0.05)
            with lk:
                cur[0] -= 1

    with cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENCY * 3) as ex:
        list(ex.map(lambda _: work(), range(MAX_CONCURRENCY * 6)))
    assert peak[0] <= MAX_CONCURRENCY, f"лимит нарушен: пик {peak[0]} > {MAX_CONCURRENCY}"
    print(f"OK: пик одновременных {peak[0]} <= лимита {MAX_CONCURRENCY}")
