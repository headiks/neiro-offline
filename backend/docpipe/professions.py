"""
Сопоставление профессий, названных моделью (проход 2), с реальным штатным расписанием.
Модель разметки уже получает список должностей компании в промпте и обязана называть
должности ДОСЛОВНО из него (см. docpipe/llm.py SECTION_SYSTEM), поэтому здесь достаточно
точного сверения без эмбеддингов: метки профессий всегда указывают на существующие должности.
"""


def staffing_positions() -> list:
    """Уникальные должности сотрудников из штатки (плейтекст). Ленивая зависимость от users."""
    import users
    seen = []
    for u in users.list_users():
        pos = (u.get("position") or "").strip()
        if pos and pos not in seen:
            seen.append(pos)
    return seen


def match_to_staffing(named: list, positions: list) -> tuple:
    """Сверяет профессии, названные моделью, со списком должностей штатки (без учёта регистра).
    Возвращает (matched_positions, prof_conf): совпавшие должности и 1.0, если хоть одна
    совпала (модель выбирает из данного ей списка, поэтому совпадение = точное). Названия
    вне штатки отбрасываются."""
    positions = [p for p in (positions or []) if (p or "").strip()]
    if not positions:
        return [], None
    pos_set = {p.casefold(): p for p in positions}

    matched = []
    for name in named or []:
        key = (name or "").strip().casefold()
        if key and key in pos_set and pos_set[key] not in matched:
            matched.append(pos_set[key])

    return matched, (1.0 if matched else None)
