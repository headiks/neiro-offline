"""Расчёт процессов по железу: разумные числа на типичных серверах, ручные переменные главнее."""
import sizing


def test_typical_servers(monkeypatch):
    for k in ("WEB_CONCURRENCY", "NEIROMASTER_RQ_WORKERS", "NEIROMASTER_DB_POOL"):
        monkeypatch.delenv(k, raising=False)
    # Нынешний прод: 4 ядра, 8 ГБ — 4 web, 2 воркера (как было вручную), пул упирается в 20.
    assert (sizing.web_workers(4, 7.7), sizing.rq_workers(4, 7.7), sizing.db_pool(4, 7.7)) == (4, 2, 20)
    # Слабый: 2 ядра, 4 ГБ — минимум 2 web и 1 воркер, без переполнения памяти.
    assert (sizing.web_workers(2, 4), sizing.rq_workers(2, 4)) == (2, 1)
    # Крупный: 16 ядер, 64 ГБ — воркеров по ядрам, соединения к БД не выше max_connections.
    web, rq, pool = sizing.web_workers(16, 64), sizing.rq_workers(16, 64), sizing.db_pool(16, 64)
    assert (web, rq) == (16, 8) and (web + rq) * pool <= sizing.PG_MAX_CONNECTIONS


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    monkeypatch.setenv("NEIROMASTER_RQ_WORKERS", "5")
    monkeypatch.setenv("NEIROMASTER_DB_POOL", "7")
    assert (sizing.web_workers(16, 64), sizing.rq_workers(2, 4), sizing.db_pool()) == (3, 5, 7)


def test_detects_something():
    assert sizing.cpus() >= 1 and sizing.mem_gb() > 0
