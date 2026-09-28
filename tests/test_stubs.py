"""
Заглушки тяжёлых зависимостей для тестов чистой логики.

Модули rag / indexing / docpipe тянут requests, psycopg и настоящий config — на машине
разработчика их может не быть, а логике, которую мы проверяем (регэкспы ЧС, отбор папок,
валидация JSON модели, разбор штатки), они не нужны. Раньше каждый тест копировал один и
тот же блок подмен; теперь он один здесь. Векторов/эмбеддингов/Qdrant в проекте больше нет —
их заглушки убраны.

Вызывать ДО импорта тестируемого модуля:

    import test_stubs
    cfg = test_stubs.install()
    import rag
"""

import sys
import types


def install(db: bool = True, psycopg: bool = False,
            stub_modules=("folders",), **config_extra) -> types.ModuleType:
    """
    Ставит заглушки и возвращает модуль-заглушку config (его можно донастроить).

    db           — подменить модуль db «пустышкой» (query/execute ничего не делают).
    psycopg      — добавить заглушки psycopg* (нужны docpipe).
    stub_modules — какие модули проекта подменить пустышкой.
    """
    for name in ("requests",) + tuple(stub_modules):
        sys.modules.setdefault(name, types.ModuleType(name))
    # requests.exceptions нужен deepseek.py на уровне модуля (_RETRYABLE) — иначе импорт
    # rag/deepseek падает ещё до тестов. Даём заглушку с нужными классами исключений.
    _req = sys.modules["requests"]
    if not hasattr(_req, "exceptions"):
        exc = types.ModuleType("requests.exceptions")
        for _e in ("RequestException", "Timeout", "ConnectionError", "SSLError", "HTTPError"):
            setattr(exc, _e, type(_e, (Exception,), {}))
        _req.exceptions = exc
        sys.modules["requests.exceptions"] = exc

    if psycopg:
        for name in ("psycopg", "psycopg.types", "psycopg.rows", "psycopg_pool"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["psycopg.types.json"] = types.ModuleType("psycopg.types.json")
        sys.modules["psycopg.types.json"].Json = lambda x: x

    if db:
        mod = types.ModuleType("db")
        mod.query = mod.execute = lambda *a, **k: None
        sys.modules.setdefault("db", mod)

    cfg = sys.modules.get("config")
    if cfg is None or not isinstance(cfg, types.ModuleType) or hasattr(cfg, "__file__"):
        cfg = types.ModuleType("config")
        sys.modules["config"] = cfg
    for k, v in config_extra.items():
        setattr(cfg, k, v)
    return cfg


if __name__ == "__main__":
    cfg = install(some_flag=1)
    assert cfg.some_flag == 1
    import requests                       # заглушка встала
    assert requests is not None
    print("test_stubs: заглушки — OK")
