"""
Нагрузочный тест: N одновременных запросов к серверу (критерий — «10 000 пользователей
нажали кнопку»; начинаем с 1000+). Только stdlib — ставить ничего не нужно.

    python loadtest.py https://host/api/me --n 1000 --cookie "nm_session=..."
    python loadtest.py https://host/ask --n 200 --post '{"question": "Где получить пропуск?"}' --cookie ...

Печатает долю ошибок и задержки p50/p95/max. /ask ходит в DeepSeek (или в кэш частых
вопросов) — его гоняйте малым N и с прогретым кэшем, иначе тест упрётся в лимиты тарифа.
"""
import _path  # noqa: F401,E402 — backend/ в sys.path
import argparse
import json
import statistics
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


def hit(url: str, cookie: str, body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET",
                                 headers={"Cookie": cookie, "Content-Type": "application/json"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = type(e).__name__
    return code, time.perf_counter() - start


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--n", type=int, default=1000, help="одновременных запросов")
    ap.add_argument("--cookie", default="")
    ap.add_argument("--post", default=None, help="JSON-тело (тогда POST)")
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args()
    body = json.dumps(json.loads(a.post)).encode() if a.post else None
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=a.n) as ex:
        res = list(ex.map(lambda _: hit(a.url, a.cookie, body, a.timeout), range(a.n)))
    wall = time.perf_counter() - t0
    codes = Counter(c for c, _ in res)
    lat = sorted(t for _, t in res)
    ok = sum(n for c, n in codes.items() if c == 200)
    print(f"запросов: {a.n} за {wall:.1f} с · успешных: {ok} ({100 * ok / a.n:.1f}%) · коды: {dict(codes)}")
    print(f"задержка p50={statistics.median(lat):.2f}с p95={lat[int(0.95 * (len(lat) - 1))]:.2f}с max={lat[-1]:.2f}с")


if __name__ == "__main__":
    main()
