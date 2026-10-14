"""
Конвертеры унифицированного сообщения плана адаптации в конкретные форматы.

Генерация (planner.generate_substage_message) выдаёт ОДИН унифицированный формат
контента (envelope), независимо от типа подэтапа:

    {
      "format": "unified/1",
      "title": str,            # краткий заголовок
      "intro": str,            # дружелюбная краткая обвязка (приветствие)
      "body":  str,            # основной текст: факты из документов, без сокращений
      "key_points": [str],     # факты списком — только те, которых нет в body
      "questions": [           # для опроса/теста
         {"text": str, "type": "single|multi|bool|open",
          "options": [{"text": str, "correct": bool}], "explanation": str}
      ],
      "checklist": [str],      # для чек-листа
      "outro": str,            # краткое завершение / к кому обратиться
      "hr_note": str           # "Уточнить в отделе кадров: ..." или "" — только для админки
    }

Отсюда бэкенд конвертирует его в нужный интерактивный формат: опрос (survey),
тест (quiz), чек-лист (checklist) или простое сообщение (message). Один источник —
много представлений, без повторной генерации.
"""

import re
from typing import Optional

# Типы подэтапов -> целевой формат конвертации.
KIND_TO_FORMAT = {
    "survey": "survey",
    "quiz": "quiz",
    "checklist": "checklist",
    "message": "message",
    "reminder": "message",
    "system_check": "checklist",
    "handover": "message",
}


def _s(value) -> str:
    return str(value or "").strip()


def _list(value) -> list:
    return value if isinstance(value, list) else []


# ---------- Читаемость текста (без модели) ----------
# Модель иногда отдаёт markdown-таблицу, перечень «1) … 2) …» в одну строку, «простыню» без
# абзацев и жаргон. Чиним детерминированно — и новые, и уже сохранённые сообщения
# (planner.rerender_saved_texts), без повторной генерации. Все шаги идемпотентны.
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_ENUM_ITEM = re.compile(r"(?:^|(?<=\s))(\d{1,2})\)\s")
_SENT_END = re.compile(r"(?:(?<=\w\w\w[.!?…])|(?<=[»)][.!?…]))\s+(?=[«\"(А-ЯЁA-Z])")
PARA_MAX = 420          # длиннее — режем абзац по предложениям


def _is_table_sep(line: str) -> bool:
    return "|" in line and "--" in line and re.fullmatch(r"[\s|:\-]+", line) is not None


def _untable(text: str) -> str:
    """Markdown-таблица -> строки «• ячейка — ячейка»: палочки в приложении нечитаемы.
    Шапку (строка перед разделителем «|---|») выбрасываем — строки понятны и без неё."""
    lines, out = text.split("\n"), []
    for i, line in enumerate(lines):
        if _is_table_sep(line):
            continue
        m = _TABLE_ROW.match(line)
        if not m:
            out.append(line)
        elif not (i + 1 < len(lines) and _is_table_sep(lines[i + 1])):
            cells = [c.strip() for c in m.group(1).split("|") if c.strip()]
            if cells:
                out.append("• " + " — ".join(cells))
    return "\n".join(out)


def _split_enums(line: str) -> str:
    """«Порядок: 1) остановите станок; 2) позовите…» -> каждый пункт с новой строки."""
    nums = _ENUM_ITEM.findall(line)
    if len(nums) < 2 or nums[:2] != ["1", "2"]:
        return line
    return re.sub(r"\s+(?=\d{1,2}\)\s)", "\n", line)


def _paragraphs(line: str) -> str:
    """Абзац длиннее PARA_MAX -> абзацы по 2–3 предложения (сплошной текст тяжело читать)."""
    if len(line) <= PARA_MAX or line.lstrip().startswith(("•", "☐", "-")):
        return line
    out, buf = [], ""
    for sent in _SENT_END.split(line.strip()):
        if buf and len(buf) + len(sent) > PARA_MAX * 0.7:
            out.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
    if buf:
        out.append(buf)
    return "\n\n".join(out)


def _cap(src: str, repl: str) -> str:
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl


_SHTATKA = {"а": "штатное расписание", "у": "штатное расписание", "и": "штатного расписания",
            "е": "штатном расписании", "ой": "штатным расписанием"}
# «должностная» как существительное: дальше знак препинания, конец или предлог — не «должностная инструкция».
_NOUN_END = r"(?=\s*(?:[.,;:!?)»—–]|$)|\s+(?:и|или|по|в|на|с|к|у|до|для|при)\b)"


def _dolzh(m) -> str:
    end = m.group(1).lower()
    if end == "ая":
        r = "должностная инструкция"
    elif end == "ую":
        r = "должностную инструкцию"
    else:
        prev = m.string[max(0, m.start() - 4):m.start()].lower()
        r = "должностной инструкцией" if re.search(r"(?:^|\s)со?\s$", prev) else "должностной инструкции"
    return _cap(m.group(0), r)


# (regex, замена) — жаргон и англицизмы -> термины, которые ждёт заказчик.
_GLOSSARY = (
    (re.compile(r"\b[Шш]татк(а|у|и|е|ой)\b"), lambda m: _cap(m.group(0), _SHTATKA[m.group(1)])),
    (re.compile(r"\b[Дд]олжностн(ая|ую|ой)\b" + _NOUN_END), _dolzh),
    (re.compile(r"\b[Пп]од наставником\b"), lambda m: _cap(m.group(0), "под руководством наставника")),
    (re.compile(r"\b[Уу] HR\b"), lambda m: _cap(m.group(0), "в отделе кадров")),
    (re.compile(r"\b[КкВв] HR\b"), lambda m: _cap(m.group(0), "в отдел кадров")),
    (re.compile(r"\b[Сс] HR\b"), lambda m: _cap(m.group(0), "с отделом кадров")),
    (re.compile(r"\bHR-специалист(\w*)"), lambda m: f"специалист{m.group(1)} по персоналу"),
    (re.compile(r"\bHR\b"), lambda m: "отдел кадров"),
)


def tidy(text: str) -> str:
    """Текст для сотрудника: без таблиц из палочек, перечни столбиком, абзацы, без жаргона."""
    text = _s(text)
    if not text:
        return text
    lines = _untable(text).split("\n")
    text = "\n".join(_paragraphs(part) for line in lines for part in _split_enums(line).split("\n"))
    for rx, repl in _GLOSSARY:
        text = rx.sub(repl, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------- Кому обратиться: кнопки «Написать наставнику» и т.п. ----------
_CONTACT_VERB = re.compile(
    r"(?i)\b(?:обратит|обращайт|обратиться|напишите|написать|пишите|позвоните|позвонить|сообщите|"
    r"сообщить|сообщайте|спросите|спросить|свяжитесь|связаться|уточните|уточнить|подойдите|"
    r"подойти|скажите|задайте|задать|позовите|начните с|к кому)"
    r"|вопрос\w*[^.!?\n]*?[—–-]\s*(?:к|в)\s")                # «вопросы по комплекту — к мастеру»
_ROLES = (
    ("mentor", re.compile(r"(?i)\bнаставни(?:к|ка|ку|ком|ке)\b"), "Написать наставнику"),
    ("manager", re.compile(r"(?i)\bруководител[ьяюеи]\w*|\bмастер\w*\s+участка"), "Написать руководителю"),
    ("hr", re.compile(r"(?i)\bотдел\w*\s+кадров|\bспециалист\w*\s+по\s+персоналу|\bHR\b"), "Написать в отдел кадров"),
)


def contact_roles(text: str) -> list:
    """Роли, к которым сотруднику предлагают обратиться. Роль считается, только если она в одном
    предложении с призывом обратиться: «практика под руководством наставника» кнопку не даёт."""
    found = set()
    for sent in re.split(r"(?<=[.!?…])\s+|\n+", text or ""):
        if _CONTACT_VERB.search(sent):
            found |= {role for role, rx, _ in _ROLES if rx.search(sent)}
    return [role for role, _, _ in _ROLES if role in found]


def contacts(text: str) -> list:
    labels = {role: label for role, _, label in _ROLES}
    return [{"role": r, "label": labels[r]} for r in contact_roles(text)]


def _stems(text: str) -> set:
    """Значимые слова по основе (первые 5 букв): «помогу/помогаю», «вопросы/вопросами» совпадут."""
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")) if len(w) > 3}


def fresh_points(text: str, points) -> list:
    """Пункты, которых ещё нет в тексте. Модель любит пересказать абзац теми же словами
    списком — это тавтология: пункт, чьи значимые слова в основном (≥60%) уже есть в тексте,
    выбрасываем. ponytail: совпадение основ слов, без морфологии — перефраз синонимами пройдёт."""
    seen = _stems(text)
    out = []
    for p in (_s(x) for x in _list(points)):
        words = _stems(p)
        if p and (not words or len(words & seen) / len(words) < 0.6):
            out.append(p)
            seen |= words
    return out


def _clean(content: dict) -> dict:
    """Копия envelope, где всё, что видит сотрудник, прошло tidy()."""
    c = dict(content or {})
    for k in ("intro", "body", "outro"):
        c[k] = tidy(c.get(k))
    for k in ("key_points", "checklist"):
        c[k] = [tidy(p) for p in _list(c.get(k))]
    qs = []
    for q in _list(c.get("questions")):
        if isinstance(q, dict):
            q = {**q, "text": tidy(q.get("text")), "explanation": tidy(q.get("explanation")),
                 "options": [{**o, "text": tidy(o.get("text"))} if isinstance(o, dict) else tidy(o)
                             for o in _list(q.get("options"))]}
        qs.append(q)
    c["questions"] = qs
    return c


def to_text(content: dict) -> str:
    """Собирает текст сообщения ДЛЯ СОТРУДНИКА из унифицированного envelope.
    Порядок: обвязка -> основной текст -> новые факты списком -> чек-лист -> завершение.
    hr_note — служебная пометка для HR, сотруднику не показывается (админка — отдельно)."""
    c = _clean(content)
    parts = [_s(c.get(k)) for k in ("intro", "body") if _s(c.get(k))]
    points = fresh_points("\n".join(parts), c.get("key_points"))
    if points:
        parts.append("\n".join(f"• {p}" for p in points))
    checklist = [_s(p) for p in _list(c.get("checklist")) if _s(p)]
    if checklist:
        parts.append("\n".join(f"☐ {p}" for p in checklist))
    if _s(c.get("outro")):
        parts.append(_s(c["outro"]))
    return "\n\n".join(parts).strip()


def _norm_options(raw) -> list:
    """Варианты ответа -> [{"id","text","correct"}]. Элемент — строка или {text,correct}."""
    out = []
    for i, opt in enumerate(_list(raw), 1):
        if isinstance(opt, dict):
            text, correct = _s(opt.get("text")), bool(opt.get("correct"))
        else:
            text, correct = _s(opt), False
        if text:
            out.append({"id": f"o{i}", "text": text, "correct": correct})
    return out


def _questions(content: dict) -> list:
    out = []
    for i, q in enumerate(_list((content or {}).get("questions")), 1):
        if not isinstance(q, dict):
            continue
        text = _s(q.get("text"))
        if not text:
            continue
        opts = _norm_options(q.get("options"))
        qtype = _s(q.get("type")) or ("open" if not opts else "single")
        if qtype == "bool" and not opts:          # «да/нет» — кнопками, а не пустым вопросом
            opts = _norm_options(["Да", "Нет"])
        out.append({
            "id": f"q{i}", "text": text, "type": qtype,
            "options": opts, "explanation": _s(q.get("explanation")),
        })
    return out


def to_message(content: dict) -> dict:
    return {"type": "message", "title": _s((content or {}).get("title")),
            "text": to_text(content)}


def to_survey(content: dict) -> dict:
    """Опрос: вопросы с вариантами, БЕЗ правильных ответов (сбор мнения)."""
    questions = [{
        "id": q["id"], "text": q["text"],
        "type": q["type"] if q["type"] in ("single", "multi", "open") else "single",
        # Вариант «Написать наставнику» — ещё и кнопка связи: открывает письмо этой роли.
        "options": [{"id": o["id"], "text": o["text"],
                     **({"contact": r[0]} if (r := contact_roles(o["text"])) else {})}
                    for o in q["options"]],
    } for q in _questions(content)]
    return {"type": "survey", "title": _s((content or {}).get("title")),
            "intro": _s((content or {}).get("intro")) or to_text(content),
            "questions": questions}


def to_quiz(content: dict) -> dict:
    """Тест: вопросы с вариантами и отметкой правильного + пояснением."""
    questions = [{
        "id": q["id"], "text": q["text"],
        "type": q["type"] if q["type"] in ("single", "multi") else "single",
        "options": [{"id": o["id"], "text": o["text"], "correct": o["correct"]} for o in q["options"]],
        "explanation": q["explanation"],
    } for q in _questions(content)]
    return {"type": "quiz", "title": _s((content or {}).get("title")),
            "intro": _s((content or {}).get("intro")) or to_text(content),
            "questions": questions}


def to_checklist(content: dict) -> dict:
    """Чек-лист: пункты с отметкой выполнения. Источник — checklist или key_points."""
    c = content or {}
    items = [_s(p) for p in _list(c.get("checklist")) if _s(p)]
    if not items:
        items = [_s(p) for p in _list(c.get("key_points")) if _s(p)]
    return {"type": "checklist", "title": _s(c.get("title")),
            "intro": _s(c.get("intro")) or _s(c.get("body")),
            "items": [{"id": f"i{i}", "text": t, "done": False} for i, t in enumerate(items, 1)]}


_CONVERTERS = {"survey": to_survey, "quiz": to_quiz,
               "checklist": to_checklist, "message": to_message}


def convert(content: dict, kind: str) -> dict:
    """Конвертирует унифицированное сообщение в формат под тип подэтапа (kind).
    contacts — к кому текст предлагает обратиться: под сообщением кнопки «Написать …»."""
    fmt = KIND_TO_FORMAT.get(kind, "message")
    c = _clean(content)
    out = _CONVERTERS[fmt](c)
    found = contacts(to_text(c))
    if found:
        out["contacts"] = found
    return out


def _demo():
    c = {
        "format": "unified/1", "title": "Охрана труда",
        "intro": "Здравствуйте, [Имя]!", "body": "Носите каску в цехе.",
        "key_points": ["Каска обязательна", "Очки при сварке"],
        "questions": [{"text": "Что обязательно в цехе?", "type": "single",
                       "options": [{"text": "Каска", "correct": True},
                                   {"text": "Ничего", "correct": False}],
                       "explanation": "Каска защищает голову."}],
        "checklist": ["Получить каску", "Пройти инструктаж"],
        "outro": "Вопросы — наставнику.", "hr_note": "",
    }
    assert "Каска обязательна" in to_text(c)
    survey = convert(c, "survey")
    assert survey["type"] == "survey"
    assert "correct" not in survey["questions"][0]["options"][0]   # опрос без правильных
    quiz = convert(c, "quiz")
    assert quiz["questions"][0]["options"][0]["correct"] is True    # тест с правильными
    chk = convert(c, "checklist")
    assert chk["items"][0]["text"] == "Получить каску" and chk["items"][0]["done"] is False
    assert convert(c, "reminder")["type"] == "message"             # напоминание -> сообщение
    # пустой контент не падает
    assert convert({}, "quiz")["questions"] == []
    print("msgconvert demo ok")


if __name__ == "__main__":
    _demo()
