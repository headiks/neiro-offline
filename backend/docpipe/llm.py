"""
LLM-слой (онлайн DeepSeek): два прохода разметки со СТРОГИМ JSON (response_format=
json_object), temperature=0 — воспроизводимо. JSON-схема передаётся модели текстом в
промпте (DeepSeek не поддерживает schema-constraint, только json_object), а строгую
валидацию/нормализацию ответа делает core.coerce_section_labels (проход 2) и
professions.match_to_staffing.
"""

import os
import json

import deepseek

MODEL = os.environ.get("DEEPSEEK_DOCPIPE_MODEL", "") or None   # None -> дефолт deepseek.MODEL
TIMEOUT = int(os.environ.get("NEIROMASTER_DOCPIPE_TIMEOUT", "300"))
PROMPT_VERSION = "docpipe-8"   # v8: границы подэтапов (ВКЛЮЧАТЬ/НЕ ВКЛЮЧАТЬ из focus) + анти-обвязка

_HEAD_TOKENS = 3000   # сколько начала документа отдаём в проход 1 (≈ символов * 3)

# Сколько символов фрагмента блока отдаём модели в промпте (должно вмещать крупный блок
# SECTION_MAX_TOKENS; ~3 символа на токен). Настраивается тем же env, что и на сервере.
FRAGMENT_CHARS = int(os.environ.get("NEIROMASTER_DOCPIPE_FRAGMENT_CHARS", "24000"))


def _chat(system: str, user: str, schema: dict) -> dict:
    # DeepSeek json_object не берёт schema-constraint — схему даём модели текстом,
    # строгую форму гарантирует нормализация в core.coerce_section_labels.
    sys_with_schema = (system + "\n\nВерни СТРОГО JSON по схеме (JSON):\n"
                       + json.dumps(schema, ensure_ascii=False))
    content = deepseek.chat(sys_with_schema, user, model=MODEL, json_mode=True,
                            temperature=0, timeout=TIMEOUT)
    return json.loads(content)


# ---------- Проход 1: карточка документа ----------
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string"},
        "summary": {"type": "string"},
        "audience": {"type": "array", "items": {"type": "string"}},
        "scope": {"type": "string", "enum": ["mono_profession", "multi_profession", "general"]},
    },
    "required": ["doc_type", "summary", "audience", "scope"],
}

CARD_SYSTEM = """Ты — аналитик корпоративной базы знаний. По заголовку, оглавлению и началу
документа составь его карточку. audience — список должностей, которым документ адресован
(или ["все"], если для всех). scope: mono_profession — документ про одну должность;
multi_profession — про несколько; general — общий для всех. Отвечай строго по схеме, по-русски."""


def doc_card(title: str, toc: str, head_text: str) -> dict:
    user = (f"Заголовок: {title}\n\nОглавление:\n{toc or '—'}\n\n"
            f"Начало документа:\n{(head_text or '')[:_HEAD_TOKENS * 3]}")
    return _chat(CARD_SYSTEM, user, CARD_SCHEMA)


# ---------- Проход 2: разметка секции ПО ЧАНКАМ ----------
# Модель делит фрагмент на логически завершённые чанки и КАЖДОМУ проставляет свои подэтапы.
# Так один релевантный чанк попадает в подэтап, даже если соседние чанки — про другое.
_CHUNK_SUBSTAGE = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["id", "confidence"],
}
SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_meaningful": {"type": "boolean"},
        "professions": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
        "chunks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "marker": {"type": "string"},
                    "substages": {"type": "array", "items": _CHUNK_SUBSTAGE},
                    "is_general": {"type": "boolean"},
                },
                "required": ["marker", "substages", "is_general"],
            },
        },
    },
    "required": ["is_meaningful", "professions", "why", "chunks"],
}

SECTION_SYSTEM = """Ты размечаешь ФРАГМЕНТ внутреннего документа относительно плана адаптации.
Тебе дают карточку документа, путь заголовков, текст фрагмента, ПОЛНЫЙ список подэтапов плана
(с id и описанием) и список должностей компании.

ГЛАВНОЕ: раздели фрагмент на логически завершённые ЧАНКИ и размечай КАЖДЫЙ чанк отдельно.
Чанк = законченная по смыслу единица, несущая информацию (правило, процедура, определение,
перечень, норма). Не разрывай мысль, предложение, пункт или таблицу посередине.

Для КАЖДОГО чанка верни объект:
- marker — ДОСЛОВНО первые 6–10 слов этого чанка (скопируй из текста без изменений, ничего не
  сокращай и не перефразируй). Первый marker — самое начало фрагмента. Маркеры идут по порядку.
- substages — все подэтапы, содержанию которых ИМЕННО ЭТОТ чанк соответствует по ТЕМЕ.
  Бери id ТОЛЬКО из списка. Обычно 1–3 подэтапа, при необходимости до 4.
  ВАЖНО — НЕ ТЕРЯЙ информацию:
  • Детали, таблицы, приложения, перечни, формулы, числа, УСЛОВИЯ и ЧАСТНЫЕ СЛУЧАИ темы
    относятся к тому же подэтапу, что и сама тема. Пример: если подэтап — «структура дохода»,
    то к нему относятся И тарифная сетка, И районные коэффициенты, И надбавки, И КТУ, И размеры
    и условия премий, И основания снижения/лишения премии, И премии к праздникам и награды.
    Размечай КАЖДЫЙ такой чанк этим подэтапом, ДАЖЕ ЕСЛИ соседний чанк уже про него.
  • Перечень документов/вещей/допусков для оформления или выхода на работу — это содержание
    подэтапа про такой чек-лист/допуски, а НЕ общая информация. Документ может быть написан
    со стороны работодателя (регламент приёма, положение о найме) — всё равно: если он
    перечисляет документы, допуски или шаги, нужные сотруднику при приёме и выходе на работу,
    относи их к соответствующему подэтапу (чек-лист документов, профессиональные допуски).
  • Одна тема может относиться к нескольким подэтапам (напр. премии к праздникам — и к доходу,
    и к нематериальной мотивации/признанию) — укажи все подходящие.
  confidence — уверенность 0..1, что чанк относится к теме подэтапа. Ставь 0.6–0.9 уверенным.
- is_general=true ТОЛЬКО для действительно служебно-вводного текста БЕЗ конкретики: преамбула
  («настоящее положение разработано в соответствии с …»), определения терминов, общие цели.
  Если в чанке есть конкретные правила, числа, условия, перечни, процедуры — это НЕ general,
  найди подходящий подэтап. Пустой substages при осмысленном содержании — почти всегда ошибка.

ГРАНИЦЫ ТЕМЫ (строго): у подэтапов в списке даны пометки «ВКЛЮЧАТЬ» и «НЕ ВКЛЮЧАТЬ».
- Размечай чанк подэтапом ТОЛЬКО если его содержание попадает в «ВКЛЮЧАТЬ» этого подэтапа.
- Если чанк подходит под «НЕ ВКЛЮЧАТЬ» подэтапа — НЕ ставь этот подэтап, даже если тема близка
  (напр. оглавление/сфера действия/комиссии колдоговора НЕ относятся к подэтапу про соцпакет).
- Чисто юридическая/административная обвязка (оглавление, реквизиты и сфера действия договора,
  состав и полномочия комиссий, порядок применения/изменения/продления ЛНА, вводные преамбулы)
  — is_general=true, substages=[]; не привязывай её к содержательным подэтапам.

Секция целиком:
- is_meaningful=false, если ВЕСЬ фрагмент служебный (заголовок, номер, оглавление) без содержания.
- professions — должности, для которых специфичен весь фрагмент. Копируй их ДОСЛОВНО из
  списка должностей компании (без изменений и перефраза); если фрагмент общий — пустой список.
- why — одно короткое предложение: о чём фрагмент.
Этапы НЕ указывай — они выводятся из подэтапов. Отвечай строго по схеме, по-русски.

Пример:
{"is_meaningful": true, "professions": [], "why": "Структура дохода, тарифы, КТУ и условия премирования.", "chunks": [{"marker": "Заработная плата состоит из оклада", "substages": [{"id":"first_day.pay_and_kpi","confidence":0.9}], "is_general": false}, {"marker": "Тарифная сетка и районные коэффициенты", "substages": [{"id":"first_day.pay_and_kpi","confidence":0.85}], "is_general": false}, {"marker": "Премия снижается при наличии дисциплинарного", "substages": [{"id":"first_day.pay_and_kpi","confidence":0.8}], "is_general": false}, {"marker": "Настоящее положение разработано в соответствии", "substages": [], "is_general": true}]}"""


def _focus_for(sid, sub):
    """Границы темы подэтапа (ИЗВЛЕЧЬ/ЗАПРЕТ) — из каталога по catalog-id, фолбэк — focus самого
    подэтапа. Даёт классификатору те же границы, что у генерации: точнее метки, меньше мусора."""
    f = sub.get("focus") if isinstance(sub.get("focus"), dict) else None
    if f:
        return f
    try:
        import planner
        fmap = planner._focus_map()
    except Exception:
        return None
    hit = fmap.get(sid)
    if not hit and sid and "." not in str(sid):
        hit = next((v for k, v in fmap.items() if k.endswith("." + str(sid))), None)
    return hit if isinstance(hit, dict) else None


def _plan_lines(structure: dict) -> str:
    lines = []
    for st in (structure or {}).get("stages") or []:
        for sub in st.get("substages") or []:
            sid = sub.get("id")
            desc = (sub.get("description") or sub.get("brief") or "").strip()
            line = f"- {sid} [{st.get('title')} / {sub.get('title')}]: {desc}"
            f = _focus_for(sid, sub)
            if f:
                if f.get("include"):
                    line += f"\n    ВКЛЮЧАТЬ (относится к теме): {f['include']}"
                if f.get("exclude"):
                    line += f"\n    НЕ ВКЛЮЧАТЬ (не относится): {f['exclude']}"
            lines.append(line)
    return "\n".join(lines)


def section_labels(section_text: str, heading_path: list, card: dict,
                   structure: dict, positions: list) -> dict:
    """Сырой JSON модели для секции (проход 2). Нормализацию делает core.coerce_section_labels."""
    user = (
        f"Карточка документа: {json.dumps(card, ensure_ascii=False)}\n"
        f"Путь заголовков: {' / '.join(heading_path or []) or '—'}\n"
        f"Должности компании: {', '.join(positions) or '—'}\n\n"
        f"Подэтапы плана:\n{_plan_lines(structure)}\n\n"
        f"Фрагмент:\n{(section_text or '')[:FRAGMENT_CHARS]}"
    )
    return _chat(SECTION_SYSTEM, user, SECTION_SCHEMA)
