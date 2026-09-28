"""
Замер качества офлайн-ответов — до отключения интернета, на своих документах.

    docker compose --profile offline up -d
    docker compose exec web python scripts/offline_eval.py --n 30

1. DeepSeek составляет N новых вопросов сотрудника по случайным подэтапам (другими словами,
   чем в базе ответов) и эталонные ответы по тем же документам.
2. Каждый вопрос проходит офлайн-цепочку (rag.handle_question при NEIROMASTER_LLM_MODE=
   offline): база готовых ответов -> локальная модель -> специалист.
3. DeepSeek оценивает ответ по эталону от 1 до 5.

Итог: доля ответов из базы / локальной модели / ушедших специалисту, средняя оценка,
доля неверных (1–2), время ответа. Подбор модели, порогов и бюджета справки — по этим
цифрам (LLM_MODEL, NEIROMASTER_QA_SIM, NEIROMASTER_OFFLINE_CONTEXT_CHARS). Ответы
локальной модели при замере в базу не пишутся. Отчёт — data/offline_eval.json.
"""
import _path  # noqa: F401,E402 — backend/ в sys.path
import argparse
import json
import os
import random
import statistics
import time

import config  # noqa: F401 — первым: грузит .env

QUESTION_SYSTEM = """По тексту внутренних документов завода придумай ОДИН вопрос нового
сотрудника, ответ на который в тексте есть. Сформулируй его своими словами, как в чате, не
копируя фразы текста. Дай эталонный ответ строго по тексту, коротко и с конкретикой.
Верни СТРОГО JSON: {"question": "...", "answer": "..."}"""

JUDGE_SYSTEM = """Оцени ответ помощника новому сотруднику по эталону. 5 — верно и полно,
4 — верно, мелкие пропуски, 3 — частично верно, 2 — в основном неверно или не по вопросу,
1 — неверно или выдумано. Верни СТРОГО JSON: {"score": 1-5, "why": "коротко"}"""


def make_questions(n: int) -> list:
    import deepseek
    import faq
    import planner
    import rag
    subs = faq._catalog_substages()
    random.shuffle(subs)
    out = []
    for key, title in subs:
        if len(out) >= n:
            break
        ctx, _ = planner._context_for([key])
        if not ctx:
            continue
        raw = deepseek.chat(QUESTION_SYSTEM, f"Тема: {title}\n\n" + "\n\n".join(ctx)[:12000],
                            json_mode=True, target="deepseek", max_tokens=800)
        item = rag.parse_json_response(raw)
        if item.get("question") and item.get("answer"):
            out.append({"substage": key, **item})
    return out


def judge(q: dict, answer: str) -> dict:
    import deepseek
    import rag
    raw = deepseek.chat(JUDGE_SYSTEM, f"Вопрос: {q['question']}\n\nЭталон: {q['answer']}\n\n"
                        f"Ответ помощника: {answer}", json_mode=True, target="deepseek", max_tokens=300)
    return rag.parse_json_response(raw)


def main():
    ap = argparse.ArgumentParser(description="Замер качества офлайн-ответов")
    ap.add_argument("--n", type=int, default=30)
    n = ap.parse_args().n
    import qacache
    import rag
    qacache.put = lambda *a, **k: None          # замер не пополняет базу ответов

    print(f"Составляю {n} вопросов (DeepSeek)…")
    qs = make_questions(n)
    os.environ["NEIROMASTER_LLM_MODE"] = "offline"
    rows = []
    for i, q in enumerate(qs, 1):
        t = time.time()
        try:
            res = rag.handle_question(q["question"])
        except Exception as e:
            res = {"answer": None, "error": str(e)}
        took = time.time() - t
        kind = "база" if res.get("cached") else ("модель" if res.get("answer") else "специалисту")
        verdict = judge(q, res["answer"]) if res.get("answer") else {"score": None, "why": "нет ответа"}
        rows.append({**q, "kind": kind, "got": res.get("answer"), "seconds": round(took, 1), **verdict})
        print(f"[{i}/{len(qs)}] {kind:11} {verdict.get('score') or '-'}  {took:5.1f}с  {q['question']}")

    scores = [r["score"] for r in rows if isinstance(r.get("score"), int)]
    by = lambda k: sum(1 for r in rows if r["kind"] == k)  # noqa: E731
    gen_t = [r["seconds"] for r in rows if r["kind"] == "модель"]
    summary = {
        "questions": len(rows), "from_base": by("база"), "local_model": by("модель"),
        "to_specialist": by("специалисту"),
        "avg_score": round(statistics.mean(scores), 2) if scores else None,
        "wrong_share": round(sum(1 for s in scores if s <= 2) / len(scores), 2) if scores else None,
        "model_seconds_median": round(statistics.median(gen_t), 1) if gen_t else None,
        "model_seconds_max": max(gen_t) if gen_t else None,
    }
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    with open("data/offline_eval.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
