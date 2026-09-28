"""
faq.py — заранее готовые ответы: топ-10 вопросов по каждому подэтапу и 2 вопроса по каждой
секции документов. DeepSeek придумывает вопросы нового сотрудника по тексту, отвечает на них
тем же промптом, что и ассистент (rag.generate_answer), и всё ложится в базу ответов
(qacache). Дальше сотрудник получает ответ мгновенно — и онлайн, и без интернета.

Запуск: после любого изменения документов (indexing.docs_changed -> очередь) и кнопкой в
админке. Идемпотентно: отпечаток входов подэтапа (текст его блоков) хранится в qa_state;
не изменился — модель не зовём. Изменился — ответы faq и model по подэтапу удаляются и
пишутся заново. Секции: у пересобранного документа новые id секций, поэтому старые ответы
по исчезнувшим секциям удаляются, а по новым — генерируются.

Офлайн (без DeepSeek) — только удаление устаревшего (например, админ удалил документ);
новое допишется при следующем выходе в онлайн. Там же обновляется индекс фрагментов
документов (docindex) — он нужен локальной модели.
"""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import db
import deepseek
import qacache

PROMPT_VERSION = "1"
TOP_N = 10
PER_SECTION = 2
MIN_SECTION_CHARS = 300
_WORKERS = int(os.environ.get("NEIROMASTER_GEN_WORKERS", "6"))

QUESTIONS_SYSTEM = """Ты готовишь базу частых вопросов нового сотрудника завода. Дан текст
внутренних документов компании по одной теме. Придумай {n} вопросов, которые новый сотрудник
реально задаст по этой теме и на которые В ЭТОМ ТЕКСТЕ ЕСТЬ конкретный ответ.
Правила:
- вопрос — живой, короткий, от первого лица, как пишут в чат («Когда выдают спецодежду?»);
- самые частые и практичные вопросы первыми: сроки, деньги, документы, куда идти, что взять,
  что запрещено, что делать если…;
- без вопросов про устройство документа («что написано в разделе 3?»);
- без вопросов, ответа на которые в тексте нет, и без повторов по смыслу.
Верни СТРОГО JSON: {{"questions": ["...", "..."]}}"""

def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _fingerprint(parts) -> str:
    # Формат ответа — префикс: qacache.init удаляет состояние старого формата.
    return f"{qacache.ANSWER_FORMAT}:{PROMPT_VERSION}:{_sha(parts)}"


def _qa_from(title: str, ctx_parts: list, n: int) -> list:
    """n вопросов по тексту + ответы на них. Ответы «сведений нет» отбрасываем."""
    import rag
    raw = rag.small_llm(QUESTIONS_SYSTEM.format(n=n),
                        f"Тема: {title}\n\nТекст:\n" + "\n\n".join(ctx_parts), "FAQ_Q")
    qs = [q.strip() for q in (rag.parse_json_response(raw).get("questions") or [])
          if isinstance(q, str) and q.strip()][:n]
    out = []
    for q in qs:
        a = rag.generate_answer(q, ctx_parts)
        if rag.has_answer(a):
            out.append({"question": q, "answer": a})
    return out


def _state() -> dict:
    return {r["key"]: r["fingerprint"] for r in (db.query("SELECT key, fingerprint FROM qa_state") or [])}


def _save_state(key: str, fp: str):
    db.execute("INSERT INTO qa_state (key, fingerprint) VALUES (%s, %s) ON CONFLICT (key) "
               "DO UPDATE SET fingerprint = EXCLUDED.fingerprint, updated_at = now()", (key, fp))


def _catalog_substages() -> list:
    import planner
    out = []
    for st in planner.load_catalog().get("stages") or []:
        for sub in st.get("substage_templates") or []:
            out.append((f"{st['id']}.{sub['id']}", f"{st.get('title')} / {sub.get('title')}"))
    return out


def refresh_substages() -> dict:
    """Топ-10 по подэтапам, у которых изменились документы."""
    import planner
    state, todo, gone = _state(), [], []
    catalog = _catalog_substages()
    for key, title in catalog:
        ctx, sources = planner._context_for([key])
        fp = _fingerprint(ctx) if ctx else ""
        if state.get(f"sub:{key}", "") != fp:
            todo.append((key, title, ctx, sources, fp))
    known = {f"sub:{k}" for k, _ in catalog}
    gone = [k[4:] for k in state if k.startswith("sub:") and k not in known]

    # Ответы на старых документах этих подэтапов больше не верны — и FAQ, и ответы модели.
    qacache.delete_for_substages([t[0] for t in todo] + gone)
    for key in gone:
        db.execute("DELETE FROM qa_state WHERE key = %s", (f"sub:{key}",))

    def one(t):
        key, title, ctx, sources, fp = t
        if ctx and deepseek.offline():
            return 0                    # состояние не пишем — догенерируется в онлайне
        if ctx:
            items = [{**qa, "substages": [key], "meta": {"sources": [{"source": s} for s in sources]}}
                     for qa in _qa_from(title, ctx, TOP_N)]
            qacache.add_many(items, "faq")
            _save_state(f"sub:{key}", fp)
            return len(items)
        db.execute("DELETE FROM qa_state WHERE key = %s", (f"sub:{key}",))
        return 0

    return {"substages": len(todo), "answers": _run(one, todo)}


def pending() -> dict:
    """Что ещё не сгенерировано (без вызова модели) — для scripts/offline_prepare.py."""
    import planner
    state = _state()
    subs = sum(1 for key, _ in _catalog_substages()
               if (ctx := planner._context_for([key])[0]) and state.get(f"sub:{key}") != _fingerprint(ctx))
    row = db.query(
        "SELECT count(*) AS n FROM sections s JOIN section_labels l ON l.section_id = s.id "
        "WHERE l.is_meaningful AND length(s.text) >= %s AND NOT EXISTS "
        "(SELECT 1 FROM qa_state q WHERE q.key = 'sec:' || s.id AND q.fingerprint = %s)",
        (MIN_SECTION_CHARS, _fingerprint("section")), "one") or {}
    return {"substages": subs, "sections": row.get("n", 0)}


def refresh_sections() -> dict:
    """По 2 вопроса на каждую содержательную секцию, для которой их ещё нет."""
    gone = db.query("DELETE FROM qa_answers WHERE source = 'section' AND section_id IS NOT NULL "
                    "AND section_id NOT IN (SELECT id FROM sections) RETURNING id") or []
    db.execute("DELETE FROM qa_state WHERE key LIKE %s "
               "AND substr(key, 5) NOT IN (SELECT id FROM sections)", ("sec:%",))
    if gone:
        qacache._bump()
    fp = _fingerprint("section")
    if deepseek.offline():
        return {"sections": 0, "answers": 0, "removed": len(gone)}
    rows = db.query(
        "SELECT s.id, s.text, s.heading_path, l.substages, d.filename FROM sections s "
        "JOIN section_labels l ON l.section_id = s.id JOIN documents d ON d.id = s.doc_id "
        "WHERE l.is_meaningful AND length(s.text) >= %s AND NOT EXISTS "
        "(SELECT 1 FROM qa_state q WHERE q.key = 'sec:' || s.id AND q.fingerprint = %s)",
        (MIN_SECTION_CHARS, fp)) or []

    def one(r):
        subs = [s.get("id") for s in (r.get("substages") or []) if isinstance(s, dict) and s.get("id")]
        title = " / ".join(r.get("heading_path") or []) or r["filename"]
        items = [{**qa, "substages": subs, "section_id": r["id"],
                  "meta": {"sources": [{"source": r["filename"]}]}}
                 for qa in _qa_from(title, [r["text"]], PER_SECTION)]
        qacache.add_many(items, "section")
        _save_state(f"sec:{r['id']}", fp)
        return len(items)

    return {"sections": len(rows), "answers": _run(one, rows), "removed": len(gone)}


def _run(fn, items) -> int:
    total = 0
    with ThreadPoolExecutor(max_workers=max(1, _WORKERS)) as ex:
        for res in ex.map(lambda it: _safe(fn, it), items):
            total += res
    return total


def _safe(fn, item) -> int:
    try:
        return fn(item)
    except Exception as e:     # одна тема упала — остальные идут; повтор при следующем запуске
        print(f"[faq] не сгенерировано: {e}")
        return 0


def refresh() -> dict:
    """Всё сразу, под замком: два воркера не генерируют одно и то же дважды."""
    from redis_conn import get_redis
    r = get_redis()
    lock = r.lock("nm:faq:lock", timeout=6 * 3600, blocking=False) if r is not None else None
    if lock is not None and not lock.acquire():
        r.set("nm:faq:again", "1")     # идущий прогон повторит себя по окончании
        return {"skipped": "уже идёт"}
    try:
        while True:
            import docindex
            res = {**refresh_substages(), **refresh_sections(), "reembedded": qacache.reembed_missing(),
                   "doc_chunks": docindex.build()}
            print(f"[faq] {res}")
            if r is None or not r.delete("nm:faq:again"):
                return res
    finally:
        if lock is not None:
            lock.release()
