"""
CLI: прогрев кэша частых вопросов (qacache) — один раз платим за ответы, дальше сотрудники
получают их мгновенно и бесплатно.

    python warm_qa_cache.py data/top_questions.txt [--position "Водитель"]

Файл — по вопросу в строке (пустые и #-комментарии пропускаются). Список стоит собирать из
реальных вопросов сотрудников (вкладка «Вопросы», журнал) и дополнять типовыми.
После загрузки/замены документов кэш сбрасывается сам — прогрев надо повторить.
"""
import _path  # noqa: F401,E402 — backend/ в sys.path
import argparse

import config  # noqa: F401 — первым: грузит .env (ключ DeepSeek, DSN БД)
import rag


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("file")
    ap.add_argument("--position", default="")
    args = ap.parse_args()
    questions = [q.strip() for q in open(args.file, encoding="utf-8")
                 if q.strip() and not q.lstrip().startswith("#")]
    for i, q in enumerate(questions, 1):
        res = rag.handle_question(q, position=args.position)
        mark = "кэш" if res.get("cached") else ("ответ" if res.get("answer") else "нет ответа")
        print(f"[{i}/{len(questions)}] {mark}: {q}")


if __name__ == "__main__":
    main()
