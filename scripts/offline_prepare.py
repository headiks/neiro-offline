"""
Готов ли сервер работать без интернета? Проверяет всё, что делается только онлайн (DeepSeek),
и с --fix дозапускает недостающее. Запускать, пока интернет есть:

    docker compose --profile offline up -d                        # скачает модели (один раз)
    docker compose exec web python scripts/offline_prepare.py        # проверка
    docker compose exec web python scripts/offline_prepare.py --fix  # догенерировать

Всё зелёное — можно ставить NEIROMASTER_LLM_MODE=offline и отключать интернет
(порядок — docs/offline.md). Код выхода 0 — готово, 1 — нет.
"""
import _path  # noqa: F401,E402 — backend/ в sys.path
import argparse

import config  # noqa: F401 — первым: грузит .env (ключ DeepSeek, DSN БД)
import requests

READY_DOC = {"indexed", "done", "confidential"}


def check_documents():
    import docregistry
    docs = docregistry.list_documents()
    bad = [f"{d['filename']} ({d.get('status')})" for d in docs if d.get("status") not in READY_DOC]
    ok = bool(docs) and not bad
    return ok, f"документов {len(docs)}" + (f", не разобраны: {', '.join(bad[:5])}" if bad else ""), None


def check_plans(fix):
    import api_plans
    import autoplan
    import planner
    import users
    pid = autoplan.get_default_plan_id()
    plan = planner.load_plan(pid) if pid else None
    if not plan:
        return False, "нет активного плана адаптации (Планы → «Сделать активным»)", None
    # Под все должности штатки и сотрудников: новичка любой должности примут и офлайн.
    positions = list(dict.fromkeys(api_plans._staffing_positions() +
                                   [(u.get("position") or "").strip() for u in users.list_users()
                                    if (u.get("position") or "").strip()]))
    est = planner.estimate_generation(plan, planner._norm_profs(positions, True))
    msg = f"план «{plan.get('title')}»: должностей {len(positions)}, осталось сгенерировать {est['llm_calls']}"
    fixer = (lambda: planner.start_generation(plan, positions=positions)) if est["llm_calls"] else None
    return est["llm_calls"] == 0, msg, fixer


def check_qa(fix):
    import faq
    import qacache
    p = faq.pending()
    stats = qacache.stats()
    msg = (f"готовых ответов {sum(stats.values())} ({', '.join(f'{k} {v}' for k, v in sorted(stats.items()))}); "
           f"не хватает: подэтапов {p['substages']}, секций {p['sections']}")
    return not p["substages"] and not p["sections"], msg, (faq.refresh if p["substages"] or p["sections"] else None)


def check_vectors(fix):
    import db
    import docindex
    import qacache
    s = docindex.stats()
    no_vec = (db.query("SELECT count(*) AS n FROM qa_answers WHERE embedding IS NULL", (), "one") or {}).get("n", 0)
    ok = s.get("sections_without_chunks", 0) == 0 and s.get("chunks") == s.get("with_vectors") and not no_vec
    msg = (f"фрагментов документов {s.get('chunks', 0)} (с векторами {s.get('with_vectors', 0)}), "
           f"секций без фрагментов {s.get('sections_without_chunks', 0)}, ответов без вектора {no_vec}")
    return ok, msg, (lambda: (docindex.build(), qacache.reembed_missing())) if not ok else None


def check_services():
    import deepseek
    import embed
    emb = embed.vectors(["проверка"]) is not None
    try:
        llm = requests.get(f"{deepseek.local_url()}/health", timeout=5).status_code == 200
    except Exception:
        llm = False
    msg = f"embed {'работает' if emb else 'НЕ ОТВЕЧАЕТ'}, llm {'работает' if llm else 'НЕ ОТВЕЧАЕТ (docker compose --profile offline up -d)'}"
    return emb and llm, msg, None


def main():
    ap = argparse.ArgumentParser(description="Проверка готовности к офлайн-режиму")
    ap.add_argument("--fix", action="store_true", help="догенерировать недостающее (нужен интернет)")
    fix = ap.parse_args().fix
    checks = [("Документы разобраны", lambda: check_documents()),
              ("Сообщения плана", lambda: check_plans(fix)),
              ("Частые вопросы", lambda: check_qa(fix)),
              ("Векторы для поиска", lambda: check_vectors(fix)),
              ("Локальные модели", lambda: check_services())]
    all_ok = True
    for title, fn in checks:
        try:
            ok, msg, fixer = fn()
        except Exception as e:
            ok, msg, fixer = False, f"ошибка проверки: {e}", None
        print(f"[{'OK' if ok else '--'}] {title}: {msg}")
        if not ok and fix and fixer:
            print(f"      догенерирую… (может занять долго)")
            try:
                print(f"      {fixer()}")
            except Exception as e:
                print(f"      не удалось: {e}")
        all_ok &= ok
    print("\nМожно отключать интернет." if all_ok else
          "\nЕщё не готово. Запустите с --fix и повторите проверку, когда фоновые задачи закончатся.")
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
