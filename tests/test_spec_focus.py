"""Проверки применённой HR-спецификации подэтапов (brief + focus{include,exclude,structure})
и рендера блока «В фокусе» в промпт генерации. Чистая логика над каталогом — без сети/БД."""

import planner


def _all_substages():
    for st in planner.load_catalog()["stages"]:
        for sub in st.get("substage_templates") or []:
            yield st, sub


def test_every_substage_has_brief_and_focus_structure():
    """Каждый подэтап несёт «Что раскрыть» (brief) и структуру диалога (focus.structure)
    из спецификации HR — иначе генерация теряет границы темы и объём."""
    bad = []
    n = 0
    for st, sub in _all_substages():
        n += 1
        focus = sub.get("focus") or {}
        if not sub.get("brief") or not focus.get("structure") or not focus.get("include"):
            bad.append(f"{st['id']}.{sub['id']}")
    assert n == 62, f"ожидали 62 подэтапа, в каталоге {n}"
    assert not bad, f"подэтапы без brief/include/structure: {bad}"


def test_focus_block_renders_structure_line():
    """_focus_block выводит строку с форматом/объёмом диалога — модель обязана её соблюдать."""
    fb = planner._focus_block("pre_onboarding", "benefits")
    assert "Формат и объём диалога" in fb
    assert "Включай" in fb and "Не включай" in fb


def test_benefits_focus_bans_boilerplate_and_caps_length():
    """Подэтап-эталон «Соцпакет»: запрет юр-обвязки и ограничение объёма (5–7 блоков)."""
    focus = planner._focus_map().get("pre_onboarding.benefits") or {}
    assert "оглавление" in focus.get("exclude", "").casefold()
    assert "5" in focus.get("structure", "")  # «5–7 блоков»


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK ", name)
    print("test_spec_focus: все проверки пройдены")
