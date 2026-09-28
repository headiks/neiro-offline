"""
Стресс-тест прода: сколько сотрудников система выдерживает одновременно.

Запуск на сервере (одна команда; тестовые сотрудники stress-XXXX создаются и в конце
удаляются вместе с их сессиями, сообщениями, токенами и вопросами):

    cd ~/neiromaster && git pull && sudo docker compose run --rm --no-deps \\
        -v "$PWD/scripts:/app/scripts" web python scripts/stress.py 2>&1 | tee ~/stress.log

Сценарии (каждый — ступенями, до первой ступени с ошибками >1% или p95 выше порога):
  1. inbox   — N сотрудников разом открывают приложение (GET /api/my/messages; приложение
               опрашивает его раз в 30 с) + устойчивый поток: максимум запросов в секунду.
  2. ask     — N сотрудников разом задают вопрос, ответ на который уже в базе ответов
               (путь без DeepSeek — потолок самой системы).
  3. ask-llm — 30 разных новых вопросов разом в настоящий DeepSeek (--llm 0 — пропустить):
               реальное время ответа и работа лимитера DEEPSEEK_MAX_CONCURRENCY.
  4. push    — N сотрудникам разом наступает сообщение плана: доставка в кабинет (БД) +
               пуш в FCM. Токены фиктивные: FCM отвечает ошибкой так же быстро, как
               доставкой, — реальный сетевой путь, но настоящим людям ничего не приходит.
  5. login   — N входов разом (scrypt; лимит 120 входов в минуту с одного IP).
  6. mix     — 60 с всё вместе: опрос кабинета, вопросы, рассылка, входы.

Генератор нагрузки работает на том же сервере (ест часть CPU) — реальный запас чуть выше.
"""
import _path  # noqa: F401,E402 — backend/ в sys.path
import argparse
import json
import os
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import config  # noqa: F401,E402 — первым: грузит окружение
import requests  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import users  # noqa: E402

PREFIX = "stress-"
PASSWORD = "Stress-" + uuid.uuid4().hex[:12]      # живёт только на время теста
CACHED_Q = "Где получить спецодежду?"
_local = threading.local()
REPORT = {}


# ---------- HTTP ----------
def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session()
    return s


def call(method, path, token=None, timeout=60, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    t = time.perf_counter()
    try:
        r = _session().request(method, ARGS.target + path, headers=headers, timeout=timeout, **kw)
        code = r.status_code
    except Exception as e:
        code = type(e).__name__
    return code, time.perf_counter() - t


def summarize(name, res, wall):
    lat = sorted(t for _, t in res)
    codes = {}
    for c, _ in res:
        codes[str(c)] = codes.get(str(c), 0) + 1
    ok = codes.get("200", 0)
    out = {"n": len(res), "ok": ok, "err_pct": round(100 * (len(res) - ok) / max(len(res), 1), 1),
           "codes": codes, "wall_s": round(wall, 2), "rps": round(len(res) / wall, 1) if wall else None,
           "p50_s": round(statistics.median(lat), 2) if lat else None,
           "p95_s": round(lat[int(0.95 * (len(lat) - 1))], 2) if lat else None,
           "max_s": round(lat[-1], 2) if lat else None, "load1": round(os.getloadavg()[0], 2)}
    print(f"  {name:28} n={out['n']:4} ok={ok:4} err={out['err_pct']:5}%  p50={out['p50_s']}с "
          f"p95={out['p95_s']}с max={out['max_s']}с  {out['rps']} rps  load={out['load1']}  {codes}",
          flush=True)
    return out


def burst(n, fn):
    """n потоков стартуют одновременно (барьер) — «все нажали разом»."""
    gate = threading.Barrier(n)

    def run(i):
        gate.wait()
        return fn(i)
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as ex:
        res = list(ex.map(run, range(n)))
    return res, time.perf_counter() - t


def sustained(workers, seconds, fn):
    """workers потоков без пауз seconds секунд — максимальная пропускная способность."""
    stop = time.perf_counter() + seconds
    res, lock = [], threading.Lock()

    def loop(i):
        while time.perf_counter() < stop:
            r = fn(i)
            with lock:
                res.append(r)
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(loop, range(workers)))
    return res, time.perf_counter() - t


def good(s, p95_limit):
    return s["err_pct"] <= 1.0 and (s["p95_s"] or 0) <= p95_limit


def steps(name, levels, fn, p95_limit, pause=3):
    """Ступени нагрузки до первой плохой. -> (последний хороший уровень, все ступени)."""
    rows, best = [], 0
    for n in levels:
        s = summarize(f"{name} x{n}", *burst(n, fn))
        rows.append({"level": n, **s})
        if not good(s, p95_limit):
            break
        best = n
        time.sleep(pause)
    return best, rows


# ---------- Тестовые сотрудники ----------
def setup(count):
    print(f"Создаю {count} тестовых сотрудников…", flush=True)
    salt, digest = users.hash_password(PASSWORD)
    ids, tokens = [], []
    for i in range(count):
        u = users.create_user({"username": f"{PREFIX}{i:04d}", "full_name": f"Стресс Тест {i:04d}",
                               "position": "Стресс-тест"})
        ids.append(u["id"])
    db.execute("UPDATE users SET salt = %s, hash = %s WHERE id = ANY(%s)", (salt, digest, ids))
    now = time.time()
    for uid in ids:                               # сессии сразу — вход проверяем отдельно
        tok = uuid.uuid4().hex + uuid.uuid4().hex
        db.execute("INSERT INTO sessions (token, user_id, created_at, seen_at) VALUES (%s,%s,%s,%s)",
                   (auth._hash_token(tok), uid, now, now))
        tokens.append(tok)
    for uid in ids:                               # фиктивный push-токен: FCM ответит ошибкой
        db.execute("INSERT INTO push_tokens (token, user_id, platform) VALUES (%s, %s, 'stress')",
                   (f"stress-fake-{uid}", uid))
    return ids, tokens


def cleanup():
    rows = db.query("SELECT id FROM users WHERE username LIKE %s", (PREFIX + "%",)) or []
    ids = [r["id"] for r in rows]
    if ids:
        for table, col in (("sessions", "user_id"), ("scheduled_messages", "employee_id"),
                           ("push_tokens", "user_id"), ("questions", "user_id"),
                           ("activity_log", "user_id")):
            db.execute(f"DELETE FROM {table} WHERE {col} = ANY(%s)", (ids,))
        db.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
    print(f"Удалено тестовых сотрудников: {len(ids)}", flush=True)


# ---------- Сценарии ----------
def scenario_inbox(tokens):
    print("\n[1] Кабинет: сотрудники разом открывают приложение", flush=True)
    best, rows = steps("inbox burst", [50, 100, 200, 400, 800],
                       lambda i: call("GET", "/api/my/messages", tokens[i % len(tokens)]), p95_limit=3)
    sus = summarize("inbox sustained x50 30с", *sustained(
        50, 30, lambda i: call("GET", "/api/my/messages", tokens[i])))
    REPORT["inbox"] = {"burst_ok": best, "steps": rows, "sustained": sus,
                       "polling_users": int(sus["rps"] * 30 * 0.7) if sus["err_pct"] <= 1 else None}


def scenario_ask(tokens):
    print("\n[2] Вопросы с готовым ответом (без DeepSeek)", flush=True)
    import qacache
    if not qacache.get(CACHED_Q):
        code, t = call("POST", "/ask", tokens[0], timeout=180, json={"question": CACHED_Q})
        print(f"  прогрев: {code} за {t:.1f}с", flush=True)
    if not qacache.get(CACHED_Q):
        # Без готового ответа каждый вопрос ушёл бы в DeepSeek — сотни платных вызовов.
        print("  ответ не попал в базу — сценарий пропущен", flush=True)
        REPORT["ask_cached"] = {"skipped": "нет готового ответа"}
        return
    best, rows = steps("ask cached burst", [25, 50, 100, 200, 400, 800],
                       lambda i: call("POST", "/ask", tokens[i % len(tokens)], timeout=120,
                                      json={"question": CACHED_Q}), p95_limit=5, pause=5)
    REPORT["ask_cached"] = {"burst_ok": best, "steps": rows}


def scenario_ask_llm(tokens, n):
    if not n:
        return
    print(f"\n[3] {n} новых вопросов разом в настоящий DeepSeek", flush=True)
    qs = [q.strip() for q in open("data/top_questions.txt", encoding="utf-8")
          if q.strip() and not q.startswith("#")]
    import qacache
    # Только ещё не отвеченные: их ответы лягут в базу как обычные ответы ассистента.
    qs = [q for q in qs if not qacache.get(q)][:n]
    s = summarize(f"ask DeepSeek x{len(qs)}", *burst(len(qs), lambda i: call(
        "POST", "/ask", tokens[-1 - i], timeout=300, json={"question": qs[i]})))
    REPORT["ask_llm"] = s


def _schedule(ids, tag):
    now = time.time()
    for uid in ids:
        db.execute("INSERT INTO scheduled_messages (id, employee_id, message_id, title, body, send_at, status) "
                   "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), 'pending')",
                   (str(uuid.uuid4()), uid, f"stress-{tag}", "Стресс-тест", "Проверочное сообщение", now - 1))


def dispatch_stress(ids):
    """Как messaging.dispatch_due, но только для тестовых сотрудников (чтобы не выпустить
    чужие наступившие сообщения раньше планировщика)."""
    import messaging
    import push
    t = time.perf_counter()
    rows = db.query("UPDATE scheduled_messages SET status = 'delivered', delivered_at = now(), "
                    "updated_at = now() WHERE status = 'pending' AND send_at <= now() "
                    "AND employee_id = ANY(%s) RETURNING id, employee_id, title, body, kind",
                    (ids,)) or []
    t_db = time.perf_counter() - t
    t = time.perf_counter()
    push.notify(messaging.group_pushes(rows))
    return len(rows), t_db, time.perf_counter() - t


def scenario_push(ids):
    print("\n[4] Рассылка: сообщение плана наступает N сотрудникам разом", flush=True)
    rows = []
    for n in (50, 200, 1000):
        if n > len(ids):
            break
        _schedule(ids[:n], f"push{n}")
        delivered, t_db, t_push = dispatch_stress(ids[:n])
        per = t_push / max(delivered, 1)
        print(f"  push x{n}: доставлено {delivered}, БД {t_db:.2f}с, пуши {t_push:.1f}с "
              f"({per * 1000:.0f} мс на пуш)", flush=True)
        rows.append({"n": n, "delivered": delivered, "db_s": round(t_db, 2), "push_s": round(t_push, 1),
                     "ms_per_push": round(per * 1000)})
        if t_push > 240:                 # дальше — только экстраполяция
            break
    REPORT["push"] = rows


def scenario_login():
    print("\n[5] Вход: N сотрудников разом вводят пароль", flush=True)
    best, rows = steps("login burst", [10, 25, 50, 100],
                       lambda i: call("POST", "/api/login", json={
                           "username": f"{PREFIX}{i:04d}", "password": PASSWORD}), p95_limit=5, pause=65)
    REPORT["login"] = {"burst_ok": best, "steps": rows}


def scenario_mix(ids, tokens):
    import qacache
    if not qacache.get(CACHED_Q):
        print("\n[6] пропущен: нет готового ответа для вопросов (см. [2])", flush=True)
        return
    base = REPORT.get("inbox", {}).get("burst_ok") or 100
    pollers = max(20, min(len(tokens) // 2, base // 2))
    askers = max(10, (REPORT.get("ask_cached", {}).get("burst_ok") or 50) // 2)
    print(f"\n[6] Всё вместе 60 с: {pollers} потоков опроса кабинета, волны по {askers} вопросов, "
          f"рассылка 200, входы", flush=True)
    stop = time.perf_counter() + 60
    out = {"inbox": [], "ask": [], "login": []}
    lock = threading.Lock()

    def poller(i):
        while time.perf_counter() < stop:
            r = call("GET", "/api/my/messages", tokens[i])
            with lock:
                out["inbox"].append(r)

    def asker(i):
        while time.perf_counter() < stop:
            r = call("POST", "/ask", tokens[pollers + i], timeout=120, json={"question": CACHED_Q})
            with lock:
                out["ask"].append(r)
            time.sleep(4)                # не упираться в лимит 20 вопросов в минуту на сотрудника

    def logins(_):
        i = 0
        while time.perf_counter() < stop:
            r = call("POST", "/api/login", json={"username": f"{PREFIX}{i:04d}", "password": PASSWORD})
            with lock:
                out["login"].append(r)
            i += 1
            time.sleep(1)

    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=pollers + askers + 2) as ex:
        futs = [ex.submit(poller, i) for i in range(pollers)]
        futs += [ex.submit(asker, i) for i in range(askers)]
        futs.append(ex.submit(logins, 0))
        time.sleep(20)
        push_ids = ids[pollers + askers:pollers + askers + 200]
        _schedule(push_ids, "mix")
        delivered, t_db, t_push = dispatch_stress(push_ids)
        for f in futs:
            f.result()
    wall = time.perf_counter() - t
    REPORT["mix"] = {k: summarize(f"mix {k}", v, wall) for k, v in out.items()}
    REPORT["mix"]["push"] = {"delivered": delivered, "db_s": round(t_db, 2), "push_s": round(t_push, 1)}
    print(f"  mix push: {delivered} за {t_push:.1f}с (БД {t_db:.2f}с)", flush=True)


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="Стресс-тест НейроМастера")
    ap.add_argument("--target", default="https://smarta-office.ru")
    ap.add_argument("--users", type=int, default=1000)
    ap.add_argument("--llm", type=int, default=30, help="вопросов в настоящий DeepSeek (0 — без)")
    ap.add_argument("--only", default="", help="через запятую: inbox,ask,llm,push,login,mix")
    ARGS = ap.parse_args()
    only = set(filter(None, ARGS.only.split(",")))
    want = lambda s: not only or s in only  # noqa: E731
    cleanup()                                    # хвосты прошлого прогона
    ids, tokens = setup(ARGS.users)
    REPORT["meta"] = {"target": ARGS.target, "users": ARGS.users, "cpus": os.cpu_count(),
                      "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        if want("inbox"):
            scenario_inbox(tokens)
        if want("ask"):
            scenario_ask(tokens)
        if want("llm"):
            scenario_ask_llm(tokens, ARGS.llm)
        if want("push"):
            scenario_push(ids)
        if want("login"):
            scenario_login()
        if want("mix"):
            scenario_mix(ids, tokens)
    finally:
        cleanup()
        print("\nREPORT_JSON " + json.dumps(REPORT, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
