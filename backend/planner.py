"""
Конструктор плана адаптации: этапы -> подэтапы -> расписание -> автоответы.

Что здесь происходит:

    каталог этапов (data/stage_catalog.json)
        -> человек в конструкторе собирает план: этапы, их длительность,
           подэтапы с описанием и временем срабатывания
        -> plan.json  — канонический машинный формат (источник истины)
        -> plan.md    — тот же план в удобном для LLM виде
        -> генерация: по каждому подэтапу LLM выбирает смысловые папки базы
           знаний, ищет в них чанки и пишет готовое сообщение сотруднику
        -> schedule.json — плоский список сообщений с временем отправки
           (формат под мессенджеры и самописное приложение)
        -> schedule.md  — человекочитаемая таблица этап / подэтап / дата и время / ответ

Время хранится ОТНОСИТЕЛЬНО даты выхода сотрудника: этап знает свою длительность
и якорь (до выхода / от даты выхода), подэтап — номер дня внутри этапа и время.
Абсолютные даты считаются подстановкой start_date, поэтому один и тот же план
переиспользуется для любого новичка.
"""

import os
import re
import json
import math
import hashlib
import time
import uuid
import threading
import folders
from concurrent.futures import ThreadPoolExecutor, as_completed
import unicodedata
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

from psycopg.types.json import Json

import db

BASE_DIR = Path(__file__).resolve().parents[1]   # корень проекта (код — в backend/)
DATA_DIR = BASE_DIR / "data"
CATALOG_PATH = DATA_DIR / "stage_catalog.json"
PLANS_DIR = DATA_DIR / "plans"
PLANS_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA_VERSION = "1.0"
DEFAULT_TIMEZONE = "Europe/Moscow"

# Сколько чанков базы знаний уходит в контекст генерации одного подэтапа
CONTEXT_CHUNKS = 6
# Бюджет символов контекста генерации (блоки подэтапа режем под окно модели DeepSeek).
CONTEXT_CHAR_BUDGET = int(os.environ.get("NEIROMASTER_GEN_CONTEXT_CHARS", "24000"))
# Параллельная генерация подэтапов (каждый — вызов DeepSeek).
_GEN_WORKERS = int(os.environ.get("NEIROMASTER_GEN_WORKERS", "6"))

UNIT_DAYS = {"hours": 0, "days": 1, "weeks": 7, "months": 30}
KIND_IDS = {"message", "checklist", "survey", "quiz", "reminder", "system_check", "handover"}

# ---------- Каталог этапов ----------
def load_catalog() -> dict:
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def catalog_stage_query(stage: dict) -> str:
    """Смысловой «запрос этапа» — заголовок + описание + брифы и теги всех его подэтапов.
    По нему чанки раскладываются по этапам (материализация «папок этапов»)."""
    parts = [stage.get("title", ""), stage.get("description", "")]
    for tpl in stage.get("substage_templates") or []:
        parts += [tpl.get("title", ""), tpl.get("brief", "")]
        parts += tpl.get("tags") or []
    return ". ".join(p for p in parts if p)


def catalog_substage_query(stage: dict, sub: dict) -> str:
    """Смысловой «запрос подэтапа» — заголовок этапа для контекста + заголовок, бриф и теги
    подэтапа. По нему чанки раскладываются по ПОДЭТАПАМ (тоньше этапа, мульти-лейбл)."""
    parts = [stage.get("title", ""), sub.get("title", ""), sub.get("brief", "")]
    parts += sub.get("tags") or []
    return ". ".join(p for p in parts if p)


# ---------- Фокус подэтапа: что включать/что нет/почему (из каталога) ----------
# Опциональное поле `focus` у подэтапа каталога: {"include","exclude","why"} или строка.
# Даёт модели точные границы темы при генерации — вместо воды конкретика по подэтапу.
_FOCUS_CACHE = None


def _focus_map() -> dict:
    global _FOCUS_CACHE
    if _FOCUS_CACHE is None:
        m = {}
        for st in (load_catalog().get("stages") or []):
            for sub in st.get("substage_templates") or []:
                if sub.get("focus"):
                    m[f"{st['id']}.{sub['id']}"] = sub["focus"]
        _FOCUS_CACHE = m
    return _FOCUS_CACHE


def _focus_block(stage_cat, sub_cat) -> str:
    """Блок «В фокусе» для user-промпта генерации: границы темы подэтапа. Пусто, если у
    подэтапа нет focus (тогда работают общие правила системного промпта)."""
    if not stage_cat or not sub_cat:
        return ""
    f = _focus_map().get(f"{stage_cat}.{sub_cat}")
    if not f:
        return ""
    if isinstance(f, str):
        return f"\nВ фокусе (строго по этому подэтапу):\n{f}\n"
    parts = ["\nВ фокусе — строго по этому подэтапу:"]
    if f.get("include"):
        parts.append(f"• Включай (относится к теме): {f['include']}")
    if f.get("exclude"):
        parts.append(f"• Не включай (не относится): {f['exclude']}")
    if f.get("structure"):
        parts.append(f"• Формат и объём диалога (соблюдай строго): {f['structure']}")
    if f.get("why"):
        parts.append(f"• Почему: {f['why']}")
    return "\n".join(parts) + "\n"


def spread_days(count: int, span: int) -> list:
    """Дни (1..span) для count подэтапов, равномерно по этапу. Раньше все подэтапы шаблона
    ставились на первый день этапа: до выхода сотруднику приходило 12 сообщений за день."""
    if span <= 1 or count <= 0:
        return [1] * max(count, 0)
    return [1 + (i * span) // count for i in range(count)]


def build_full_template(title: str = "") -> dict:
    """Стандартный план (≈3 месяца) из ВСЕГО каталога: все этапы и подэтапы с описаниями,
    длительностями и временем, подэтапы разнесены по дням этапа. Профессионально-независимый —
    специфику даёт база знаний при генерации. Возвращает нормализованный план (без сохранения)."""
    cat = load_catalog()
    raw = {
        "title": (title or "").strip() or "Стандартный план адаптации",
        "role": "",
        "description": "Стандартный план адаптации: все этапы и подэтапы каталога. "
                       "Единый для всех профессий — специфику даёт база знаний при генерации. Редактируйте под задачу.",
        "stages": [],
    }
    for st in cat.get("stages") or []:
        duration = dict(st.get("default_duration") or {"value": 1, "unit": "days"})
        templates = st.get("substage_templates") or []
        days = spread_days(len(templates), stage_span_days(duration))
        raw_stage = {
            "catalog_id": st["id"],
            "title": st.get("title", ""),
            "description": st.get("description", ""),
            "anchor": st.get("anchor", "from_start"),
            "duration": duration,
            "substages": [],
        }
        for tpl, day in zip(templates, days):
            raw_stage["substages"].append({
                "catalog_id": tpl["id"],
                "title": tpl.get("title", ""),
                "kind": tpl.get("kind", "message"),
                "brief": tpl.get("brief", ""),
                "tags": tpl.get("tags") or [],
                "source": "template",
                "schedule": {"day": day, "time": tpl.get("default_time", "09:00")},
            })
        raw["stages"].append(raw_stage)
    return normalize_plan(raw)


# ---------- Длительности и расчёт дат ----------
def stage_span_days(duration: dict) -> int:
    """
    Сколько календарных дней занимает этап.
    Для единицы «часы» дней выбора нет — этап укладывается в минимально
    необходимое число суток (12 ч -> 1 день, 36 ч -> 2 дня).
    """
    value = max(1, int(duration.get("value") or 1))
    unit = duration.get("unit") or "days"
    if unit == "hours":
        return max(1, math.ceil(value / 24))
    return max(1, value * UNIT_DAYS.get(unit, 1))


def stage_day_choice(duration: dict) -> bool:
    """Можно ли выбирать день внутри этапа (для «часов» — только время)."""
    return (duration.get("unit") or "days") != "hours"


def compute_offsets(plan: dict) -> dict:
    """
    Возвращает {stage_id: смещение первого дня этапа в днях от даты выхода}.

    Этапы с якорем before_start выстраиваются подряд так, чтобы последний
    их день приходился на день перед выходом (-1). Этапы from_start идут
    подряд начиная с самого дня выхода (0).
    """
    stages = plan.get("stages") or []
    before = [s for s in stages if s.get("anchor") == "before_start"]
    after = [s for s in stages if s.get("anchor") != "before_start"]

    offsets = {}

    cursor = -sum(stage_span_days(s.get("duration", {})) for s in before)
    for stage in before:
        offsets[stage["id"]] = cursor
        cursor += stage_span_days(stage.get("duration", {}))

    cursor = 0
    for stage in after:
        offsets[stage["id"]] = cursor
        cursor += stage_span_days(stage.get("duration", {}))

    return offsets


def parse_start_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_schedule(plan: dict) -> list:
    """
    Разворачивает план в плоский хронологический список позиций расписания.
    Каждая позиция уже знает своё смещение в днях и, если задана дата выхода,
    абсолютные дату и время отправки.
    """
    offsets = compute_offsets(plan)
    start = parse_start_date(plan.get("start_date"))
    items = []

    for stage_index, stage in enumerate(plan.get("stages") or [], start=1):
        stage_offset = offsets.get(stage["id"], 0)
        day_choice = stage_day_choice(stage.get("duration", {}))
        span = stage_span_days(stage.get("duration", {}))

        for sub_index, sub in enumerate(stage.get("substages") or [], start=1):
            schedule = sub.get("schedule") or {}
            day = 1 if not day_choice else max(1, min(span, int(schedule.get("day") or 1)))
            send_time = schedule.get("time") or "09:00"
            offset_days = stage_offset + (day - 1)

            send_at = None
            if start:
                hh, mm = _parse_time(send_time)
                send_at = datetime.combine(start + timedelta(days=offset_days),
                                           datetime.min.time()).replace(hour=hh, minute=mm)

            items.append({
                "message_id": f"{stage['id']}.{sub['id']}",
                "stage": {
                    "id": stage["id"],
                    "catalog_id": stage.get("catalog_id"),
                    "order": stage_index,
                    "title": stage.get("title", ""),
                },
                "substage": {
                    "id": sub["id"],
                    "catalog_id": sub.get("catalog_id"),
                    "order": sub_index,
                    "title": sub.get("title", ""),
                    "kind": sub.get("kind", "message"),
                    "brief": sub.get("brief", ""),
                },
                "schedule": {
                    "anchor": stage.get("anchor", "from_start"),
                    "stage_day": day,
                    "time": send_time,
                    "offset_days": offset_days,
                    "send_at": send_at.isoformat(timespec="minutes") if send_at else None,
                },
            })

    if plan.get("group_daily"):
        _group_by_day(items, start)
    items.sort(key=lambda i: (i["schedule"]["offset_days"], i["schedule"]["time"],
                             i["stage"]["order"], i["substage"]["order"]))
    return items


def _group_by_day(items: list, start: Optional[date]):
    """«Одна сессия в день»: все сообщения дня приходят вместе, во время первого из них,
    одним уведомлением — вместо россыпи через каждые 1–2 часа. Порядок внутри дня прежний."""
    first = {}
    for it in items:
        sch = it["schedule"]
        day = sch["offset_days"]
        first[day] = min(first.get(day, sch["time"]), sch["time"])
    for it in items:
        sch = it["schedule"]
        sch["time"] = first[sch["offset_days"]]
        if start:
            hh, mm = _parse_time(sch["time"])
            sch["send_at"] = datetime.combine(start + timedelta(days=sch["offset_days"]),
                                              datetime.min.time()).replace(hour=hh, minute=mm) \
                .isoformat(timespec="minutes")


def _parse_time(value: str) -> tuple:
    try:
        hh, mm = str(value).split(":")[:2]
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except (ValueError, AttributeError):
        return 9, 0


# ---------- Нормализация плана из конструктора ----------
def _slug(value: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    out = [c if c.isalnum() else "_" for c in text]
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug[:40] or fallback


# Идентификаторы этапов/подэтапов приходят от клиента и дальше попадают в URL и разметку
# админки — только буквы, цифры, «_» и «-» (иначе через id плана можно внедрить скрипт).
_ID_RE = re.compile(r"[^\w-]+")
# Потолок длительности этапа по единицам: защита от «этапа на миллиард дней» (переполнение дат).
MAX_DURATION = {"hours": 24 * 30, "days": 730, "weeks": 104, "months": 24}
MAX_STAGES = 50
MAX_SUBSTAGES = 200
TEXT_MAX = 4000


def _safe_id(value) -> Optional[str]:
    """Допустимый id возвращается как есть (байт в байт — иначе сгенерированные тексты потеряли
    бы привязку к подэтапу), недопустимые символы заменяются на «_»."""
    text = _ID_RE.sub("_", str(value or ""))[:120]
    return text if any(c.isalnum() for c in text) else None


def _catalog_ref(value) -> Optional[str]:
    return _safe_id(value) if value else None


def _clip(value, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def normalize_plan(raw: dict, plan_id: Optional[str] = None) -> dict:
    """Приводит присланный фронтендом план к каноническому виду и чинит очевидное."""
    now = datetime.now().isoformat(timespec="seconds")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id or _safe_id(raw.get("plan_id")) or str(uuid.uuid4()),
        "title": _clip(raw.get("title")) or "План адаптации",
        "role": _clip(raw.get("role")),
        "description": _clip(raw.get("description"), TEXT_MAX),
        "start_date": str(raw.get("start_date") or "")[:10] or None,
        "timezone": raw.get("timezone") if _valid_tz(raw.get("timezone")) else DEFAULT_TIMEZONE,
        # Все сообщения одного дня — одной «сессией» (в одно время, одним уведомлением).
        "group_daily": bool(raw.get("group_daily")),
        "created_at": raw.get("created_at") or now,
        "updated_at": now,
        "stages": [],
    }

    used_stage_ids = set()
    for s_index, raw_stage in enumerate((raw.get("stages") or [])[:MAX_STAGES], start=1):
        if not isinstance(raw_stage, dict):
            continue
        stage_id = _safe_id(raw_stage.get("id")) or f"st{s_index}_{_slug(raw_stage.get('catalog_id') or raw_stage.get('title'), f'stage{s_index}')}"
        while stage_id in used_stage_ids:
            stage_id = f"{stage_id}_{s_index}"
        used_stage_ids.add(stage_id)

        duration = raw_stage.get("duration") if isinstance(raw_stage.get("duration"), dict) else {}
        unit = duration.get("unit") if duration.get("unit") in UNIT_DAYS else "days"
        try:
            value = max(1, min(MAX_DURATION[unit], int(duration.get("value") or 1)))
        except (TypeError, ValueError):
            value = 1

        stage = {
            "id": stage_id,
            "catalog_id": _catalog_ref(raw_stage.get("catalog_id")),
            "order": s_index,
            "title": _clip(raw_stage.get("title")) or f"Этап {s_index}",
            "description": _clip(raw_stage.get("description"), TEXT_MAX),
            "anchor": "before_start" if raw_stage.get("anchor") == "before_start" else "from_start",
            "duration": {"value": value, "unit": unit},
            "substages": [],
        }

        span = stage_span_days(stage["duration"])
        day_choice = stage_day_choice(stage["duration"])

        used_sub_ids = set()
        raw_subs = [x for x in (raw_stage.get("substages") or [])[:MAX_SUBSTAGES] if isinstance(x, dict)]
        for sub_index, raw_sub in enumerate(raw_subs, start=1):
            sub_id = _safe_id(raw_sub.get("id")) or f"s{sub_index}_{_slug(raw_sub.get('catalog_id') or raw_sub.get('title'), f'sub{sub_index}')}"
            while sub_id in used_sub_ids:
                sub_id = f"{sub_id}_{sub_index}"
            used_sub_ids.add(sub_id)

            raw_schedule = raw_sub.get("schedule") or {}
            try:
                day = int(raw_schedule.get("day") or 1)
            except (TypeError, ValueError):
                day = 1
            day = 1 if not day_choice else max(1, min(span, day))
            hh, mm = _parse_time(raw_schedule.get("time") or "09:00")

            kind = raw_sub.get("kind") if raw_sub.get("kind") in KIND_IDS else "message"
            stage["substages"].append({
                "id": sub_id,
                "catalog_id": _catalog_ref(raw_sub.get("catalog_id")),
                "order": sub_index,
                "title": _clip(raw_sub.get("title")) or f"Подэтап {sub_index}",
                "kind": kind,
                "brief": _clip(raw_sub.get("brief"), TEXT_MAX),
                "source": "manual" if raw_sub.get("source") == "manual" else "template",
                "tags": [t[:100] for t in (raw_sub.get("tags") or []) if isinstance(t, str)][:30],
                "schedule": {"day": day, "time": f"{hh:02d}:{mm:02d}"},
                # Свой подэтап (без catalog_id): темы каталога, по которым берутся документы.
                # Подбираются один раз при сохранении (assign_topics), topic_src — от чего.
                "topic_keys": [k[:200] for k in (raw_sub.get("topic_keys") or []) if isinstance(k, str)][:10],
                "topic_src": str(raw_sub.get("topic_src") or "")[:64],
            })

        plan["stages"].append(stage)

    return plan


def _valid_tz(name) -> bool:
    if not name or not isinstance(name, str) or len(name) > 64:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return False


class PlanConflict(Exception):
    """План успели изменить с момента, как его открыл этот администратор."""

    def __init__(self, current: dict):
        super().__init__("План изменён другим администратором")
        self.current = current


def check_not_modified(existing: dict, expected_updated_at: Optional[str]):
    """Оптимистичная блокировка: несколько админов правят один план — сохранение поверх чужой
    правки не проходит молча (раньше выигрывала последняя запись и чужие изменения терялись)."""
    if expected_updated_at and existing.get("updated_at") and existing["updated_at"] != expected_updated_at:
        raise PlanConflict(existing)


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def assign_topics(plan: dict, prev: Optional[dict] = None, router=None) -> dict:
    """Своим подэтапам (без catalog_id) подбирает темы каталога, по которым генерация берёт
    документы. Без этого такие подэтапы всегда уходили в «пропущено»: документы размечены
    только темами каталога. Модель зовётся ОДИН раз на подэтап — пока не поменялись его
    название/бриф (topic_src), темы переносятся из прежней версии плана."""
    if router is None:
        from rag import route_substages as router
    prev_subs = {s.get("id"): s for st in (prev or {}).get("stages") or [] for s in st.get("substages") or []}
    for stage in plan.get("stages") or []:
        for sub in stage.get("substages") or []:
            if stage.get("catalog_id") and sub.get("catalog_id"):
                continue
            src = _sha([sub.get("title"), sub.get("brief")])[:16]
            old = prev_subs.get(sub.get("id")) or {}
            if sub.get("topic_src") == src and sub.get("topic_keys"):
                continue
            if old.get("topic_src") == src and old.get("topic_keys"):
                sub["topic_keys"], sub["topic_src"] = list(old["topic_keys"]), src
                continue
            text = ". ".join(p for p in (stage.get("title"), sub.get("title"), sub.get("brief")) if p)
            sub["topic_keys"] = list(router(text, top=2) or [])
            sub["topic_src"] = src
    return plan


# ---------- Хранилище планов ----------
def plan_dir(plan_id: str) -> Path:
    safe = "".join(c for c in str(plan_id or "") if c.isalnum() or c in "-_")
    # Пустой или состоящий только из «-_» id схлопывается в сам PLANS_DIR — тогда
    # delete_plan снёс бы ВСЕ планы разом (rglob+rmdir по корню). Отвергаем такие id.
    if not safe.strip("-_"):
        raise ValueError(f"Некорректный идентификатор плана: {plan_id!r}")
    resolved = (PLANS_DIR / safe).resolve()
    if resolved.parent != PLANS_DIR.resolve():
        raise ValueError(f"Некорректный идентификатор плана: {plan_id!r}")
    return resolved


def save_plan(plan: dict) -> dict:
    """Сохраняет канонический план в БД (таблица plans, JSONB). Источник истины."""
    db.execute(
        "INSERT INTO plans (plan_id, data, updated_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (plan_id) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
        (plan["plan_id"], Json(plan), datetime.now().isoformat(timespec="seconds")),
    )
    return plan


def load_plan(plan_id: str) -> Optional[dict]:
    row = db.query("SELECT data FROM plans WHERE plan_id = %s", (plan_id,), fetch="one")
    return row["data"] if row else None


def duplicate_plan(plan_id: str, new_title: Optional[str] = None) -> Optional[dict]:
    """
    Копия плана под смежную должность: та же структура этапов/подэтапов, но новый
    plan_id и заголовок. Сгенерированные тексты и расписание НЕ копируются — их
    перегенерируют под новую должность (иначе в копии остались бы ответы про старую).
    """
    src = load_plan(plan_id)
    if src is None:
        return None
    src = dict(src)
    src.pop("plan_id", None)     # normalize_plan выдаст свежий id
    src.pop("created_at", None)  # и свежие даты
    src["title"] = (new_title or "").strip() or f"{src.get('title', 'План адаптации')} (копия)"
    return save_plan(normalize_plan(src))


def refresh_from_catalog(plan_id: str) -> Optional[dict]:
    """Подтягивает в существующий план свежие описания этапов и брифы подэтапов из
    каталога (по catalog_id). Обновляет ТОЛЬКО текстовые описания:
      - у этапа: description;
      - у подэтапа с source='template' и известным catalog_id: brief и tags.
    Расписание (день/время), порядок, kind, заголовки и ручные подэтапы (source='manual')
    не трогаются. Возвращает обновлённый план или None, если плана нет.
    Нужно после расширения каталога: старые планы хранят свою копию текстов."""
    plan = load_plan(plan_id)
    if plan is None:
        return None
    cat = load_catalog()
    stage_by_id = {st["id"]: st for st in (cat.get("stages") or [])}
    sub_by_key = {f"{st['id']}.{sub['id']}": sub
                  for st in (cat.get("stages") or [])
                  for sub in (st.get("substage_templates") or [])}

    changed = 0
    for stage in plan.get("stages") or []:
        cst = stage_by_id.get(stage.get("catalog_id"))
        if cst and cst.get("description") and stage.get("description") != cst["description"]:
            stage["description"] = cst["description"]
            changed += 1
        for sub in stage.get("substages") or []:
            if sub.get("source") == "manual":
                continue
            ckey = f"{stage.get('catalog_id')}.{sub.get('catalog_id')}"
            csub = sub_by_key.get(ckey)
            if not csub:
                continue
            if csub.get("brief") and sub.get("brief") != csub["brief"]:
                sub["brief"] = csub["brief"]
                changed += 1
            if csub.get("tags") and sub.get("tags") != csub["tags"]:
                sub["tags"] = list(csub["tags"])

    plan["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_plan(plan)
    plan["_refreshed"] = changed
    return plan


def list_plans() -> list:
    rows = db.query(
        "SELECT p.data AS data, "
        "EXISTS (SELECT 1 FROM plan_schedules s WHERE s.plan_id = p.plan_id) AS generated "
        "FROM plans p"
    )
    plans = []
    for r in rows or []:
        plan = r["data"] or {}
        plans.append({
            "plan_id": plan.get("plan_id"),
            "title": plan.get("title"),
            "role": plan.get("role"),
            "start_date": plan.get("start_date"),
            "stages": len(plan.get("stages") or []),
            "substages": sum(len(s.get("substages") or []) for s in plan.get("stages") or []),
            "updated_at": plan.get("updated_at"),
            "generated": r["generated"],
        })
    return sorted(plans, key=lambda p: p.get("updated_at") or "", reverse=True)


def delete_plan(plan_id: str) -> bool:
    row = db.query("DELETE FROM plans WHERE plan_id = %s RETURNING plan_id", (plan_id,), fetch="one")
    return row is not None


def profession_slug(position: str) -> str:
    """Короткий стабильный slug должности (для UI/экспорта; в БД ключ — сама строка)."""
    return _slug(position, "obshiy")


def load_schedule(plan_id: str, profession: str = "") -> Optional[dict]:
    """Расписание плана из БД. При указанной должности берём её расписание; если его нет —
    откат на общее (profession=''). Так план один, а контент — под профессию из аккаунта."""
    prof = (profession or "").strip()
    row = db.query("SELECT data FROM plan_schedules WHERE plan_id = %s AND profession = %s",
                   (plan_id, prof), fetch="one")
    if row is None and prof:
        row = db.query("SELECT data FROM plan_schedules WHERE plan_id = %s AND profession = ''",
                       (plan_id,), fetch="one")
    return row["data"] if row else None


def list_schedule_professions(plan_id: str) -> list:
    """Профессии, под которые сгенерированы расписания (кроме общего profession='')."""
    rows = db.query(
        "SELECT profession FROM plan_schedules WHERE plan_id = %s AND profession <> '' "
        "ORDER BY profession", (plan_id,))
    return [{"slug": profession_slug(r["profession"]), "profession": r["profession"]}
            for r in (rows or [])]


def migrate_plans_from_files() -> int:
    """Разовый перенос файловых планов (data/plans/<id>/) в БД. Идемпотентно: план,
    уже присутствующий в БД, пропускаем (чтобы правки из БД не затирались файлами)."""
    if not PLANS_DIR.exists():
        return 0
    moved = 0
    for directory in PLANS_DIR.iterdir():
        if not directory.is_dir():
            continue
        plan_path = directory / "plan.json"
        if not plan_path.exists():
            continue
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = plan.get("plan_id")
        if not pid or load_plan(pid) is not None:
            continue                       # уже в БД — не трогаем
        save_plan(plan)
        moved += 1
        # общее расписание
        sched = directory / "schedule.json"
        if sched.exists():
            try:
                save_schedule(pid, json.loads(sched.read_text(encoding="utf-8")), "")
            except (json.JSONDecodeError, OSError):
                pass
        # расписания под профессии
        for f in (directory / "schedules").glob("*.json") if (directory / "schedules").exists() else []:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                save_schedule(pid, data, data.get("profession", ""))
            except (json.JSONDecodeError, OSError):
                pass
    return moved


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------- Рендер плана в Markdown (формат для LLM) ----------
def _duration_label(duration: dict) -> str:
    titles = {"hours": ("час", "часа", "часов"), "days": ("день", "дня", "дней"),
              "weeks": ("неделя", "недели", "недель"), "months": ("месяц", "месяца", "месяцев")}
    value = duration.get("value", 1)
    forms = titles.get(duration.get("unit", "days"), titles["days"])
    if value % 10 == 1 and value % 100 != 11:
        word = forms[0]
    elif value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        word = forms[1]
    else:
        word = forms[2]
    return f"{value} {word}"


def render_plan_md(plan: dict) -> str:
    """План в виде, удобном для чтения LLM: заголовки, метаданные, подэтапы списком."""
    offsets = compute_offsets(plan)
    lines = [
        f"# {plan.get('title', 'План адаптации')}",
        "",
        f"- **Идентификатор плана:** `{plan.get('plan_id')}`",
        f"- **Должность / роль:** {plan.get('role') or 'не указана'}",
        f"- **Дата выхода сотрудника:** {plan.get('start_date') or 'не задана (план относительный)'}",
        f"- **Часовой пояс:** {plan.get('timezone')}",
        f"- **Этапов:** {len(plan.get('stages') or [])}",
        f"- **Версия схемы:** {plan.get('schema_version')}",
    ]
    if plan.get("description"):
        lines += ["", plan["description"]]

    lines += ["", "Время подэтапов задано относительно даты выхода сотрудника: "
                  "этап имеет якорь (до выхода / от даты выхода) и длительность, "
                  "подэтап — номер дня внутри этапа и время суток.", ""]

    for stage in plan.get("stages") or []:
        offset = offsets.get(stage["id"], 0)
        anchor = "до выхода на работу" if stage.get("anchor") == "before_start" else "от даты выхода"
        span = stage_span_days(stage.get("duration", {}))
        lines += [
            f"## Этап {stage.get('order')}. {stage.get('title')}",
            "",
            f"- **id:** `{stage['id']}`",
            f"- **Длительность:** {_duration_label(stage.get('duration', {}))} ({span} кал. дн.)",
            f"- **Якорь:** {anchor}",
            f"- **Смещение первого дня этапа:** {offset:+d} дн. от даты выхода",
        ]
        if stage.get("description"):
            lines += [f"- **Описание:** {stage['description']}"]
        lines += [""]

        if not stage.get("substages"):
            lines += ["_Подэтапы не заданы._", ""]
            continue

        for sub in stage["substages"]:
            schedule = sub.get("schedule") or {}
            when = f"день {schedule.get('day', 1)}, {schedule.get('time', '09:00')}"
            if not stage_day_choice(stage.get("duration", {})):
                when = f"{schedule.get('time', '09:00')} (этап задан в часах — день не выбирается)"
            lines += [
                f"### Подэтап {sub.get('order')}. {sub.get('title')}",
                "",
                f"- **id:** `{sub['id']}`",
                f"- **Тип:** {sub.get('kind')}",
                f"- **Когда:** {when}",
                f"- **Смещение от даты выхода:** {offset + int(schedule.get('day', 1)) - 1:+d} дн.",
                f"- **Что должен написать бот:** {sub.get('brief') or '—'}",
            ]
            if sub.get("tags"):
                lines += [f"- **Темы для поиска в базе знаний:** {', '.join(sub['tags'])}"]
            lines += [""]

    return "\n".join(lines)


# ---------- Рендер результата генерации ----------
def render_schedule_md(schedule: dict) -> str:
    plan_title = schedule.get("plan_title", "План адаптации")
    employee = schedule.get("employee") or {}
    header = f"# Расписание адаптации — {employee['full_name']}" if employee.get("full_name") \
        else f"# Расписание автоответов — {plan_title}"

    lines = [header, ""]
    if employee:
        lines += [
            f"- **Сотрудник:** {employee.get('full_name')}",
            f"- **Должность:** {employee.get('position') or 'не указана'}",
            f"- **Подразделение:** {employee.get('department') or 'не указано'}",
            f"- **Наставник:** {employee.get('mentor') or 'не назначен'}",
            f"- **Руководитель:** {employee.get('manager') or 'не указан'}",
            f"- **План-шаблон:** {plan_title}",
        ]
    lines += [
        f"- **Идентификатор плана:** `{schedule.get('plan_id')}`",
        f"- **Должность / роль плана:** {schedule.get('role') or 'не указана'}",
        f"- **Дата выхода сотрудника:** {schedule.get('start_date') or 'не задана (даты относительные)'}",
        f"- **Часовой пояс:** {schedule.get('timezone')}",
        f"- **Сообщений:** {len(schedule.get('messages') or [])}",
        f"- **Сгенерировано:** {schedule.get('generated_at')}",
        "",
        "## Сводка",
        "",
        "| Этап | Подэтап | Дата и время отправки | Тип |",
        "|---|---|---|---|",
    ]
    for msg in schedule.get("messages") or []:
        lines.append(
            f"| {msg['stage']['order']}. {msg['stage']['title']} "
            f"| {msg['substage']['order']}. {msg['substage']['title']} "
            f"| {_when_label(msg)} | {msg['substage']['kind']} |"
        )

    lines += ["", "## Сообщения", ""]
    for msg in schedule.get("messages") or []:
        lines += [
            f"### {msg['stage']['title']} → {msg['substage']['title']}",
            "",
            f"- **id сообщения:** `{msg['message_id']}`",
            f"- **Дата и время отправки:** {_when_label(msg)}",
            f"- **Тип:** {msg['substage']['kind']}",
            f"- **Статус:** {msg.get('status')}",
        ]
        if msg.get("topics_used"):
            lines.append(f"- **Темы базы знаний:** {', '.join(msg['topics_used'])}")
        if msg.get("sources"):
            srcs = ", ".join(sorted({s.get("source") or "?" for s in msg["sources"]}))
            lines.append(f"- **Документы-источники:** {srcs}")
        if msg.get("error"):
            lines.append(f"- **Ошибка:** {msg['error']}")
        lines += ["", "**Ответ:**", "", (msg.get("content", {}).get("text") or "—"), ""]

    return "\n".join(lines)


def _when_label(msg: dict) -> str:
    schedule = msg.get("schedule") or {}
    if schedule.get("send_at"):
        return schedule["send_at"].replace("T", " ")
    anchor = "до выхода" if schedule.get("anchor") == "before_start" else "от выхода"
    return f"{schedule.get('offset_days'):+d} дн. ({anchor}), {schedule.get('time')}"


# ---------- Генерация автоответов ----------
PICK_TOPICS_SYSTEM = """
Ты — навигатор по корпоративной базе знаний. Тебе дают описание подэтапа программы
адаптации сотрудника и список тем (папок) с документами.

Задача: выбрать темы, в которых лежат документы, нужные чтобы написать этот подэтап.
Выбирай от 1 до 3 тем, самые релевантные. Если ни одна тема явно не подходит,
выбери одну наиболее близкую.

Верни ТОЛЬКО JSON со slug'ами тем: {"topics": ["slug-temy", "drugoy-slug"]}
"""

# Что дополнительно требуется в унифицированном сообщении под конкретный тип подэтапа.
# Общий envelope один; здесь — акцент, какие интерактивные поля заполнять.
KIND_GEN_HINT = {
    "message": "Обычное сообщение. Заполни intro, body, outro. key_points — ТОЛЬКО факты, которых нет в body "
               "(перечень, шаги, суммы); пересказывать body списком нельзя — тогда key_points пустой. "
               "questions и checklist оставь пустыми.",
    "reminder": "Короткое напоминание. Сделай body в 1–3 предложения, key_points пустой, без questions и checklist.",
    "checklist": "Чек-лист. Заполни checklist проверяемыми пунктами (действие в каждом). questions оставь пустым.",
    "survey": "Опрос. Заполни questions (2–5 вопросов, type single/multi/open, варианты БЕЗ поля correct — мнение, а не проверка).",
    "quiz": "Тест. Заполни questions (1–3 вопроса), к каждому 3 варианта, у верных options.correct=true, добавь explanation.",
    "system_check": "Проверка условий. Заполни checklist проверяемыми условиями с пороговыми значениями.",
    "handover": "Задача ответственному (наставник/руководитель/HR). В body: что сделать, к какому сроку, что зафиксировать.",
}

GENERATE_SYSTEM = """
Ты — НейроМастер, виртуальный наставник нового сотрудника производственной компании.
Готовишь ОДНО сообщение программы адаптации в едином машинном формате (JSON).

Верни ТОЛЬКО валидный JSON без пояснений, строго такой структуры:
{
  "title": "краткий заголовок подэтапа",
  "intro": "короткая дружелюбная обвязка-приветствие (1 предложение)",
  "body": "основной текст: только суть по теме, КРАТКО, по структуре подэтапа (см. «В фокусе»)",
  "key_points": ["факт, которого НЕТ в body", "..."],
  "questions": [
    {"text": "вопрос", "type": "single|multi|open|bool",
     "options": [{"text": "вариант", "correct": true}], "explanation": "пояснение"}
  ],
  "checklist": ["проверяемый пункт", "..."],
  "outro": "короткое завершение / к кому обратиться",
  "hr_note": "Уточнить у HR: ... (или пустая строка)"
}

ГЛАВНОЕ — КОНКРЕТИКА ПО ЭТОМУ ПОДЭТАПУ, НЕ ОБЩИЕ СЛОВА:
- Сообщение посвящено ОДНОЙ теме — теме этого подэтапа. Сначала определи её точный предмет
  (из заголовка, «что должен написать бот» и блока «В фокусе»), затем вытащи из фрагментов
  именно то, что её раскрывает.
- ЧТО ВКЛЮЧАТЬ (относится к теме): конкретные числа, суммы, проценты, сроки, условия, пороги,
  шаги, перечни, названия документов/должностей, частные случаи и исключения ПО ТЕМЕ.
- ЧТО НЕ ВКЛЮЧАТЬ (не относится): вводные преамбулы, общая миссия/ценности, определения не по
  теме и сведения про ДРУГИЕ подэтапы, случайно попавшие во фрагменты. Это вода — выбрасывай.
- ДОЛЖНОСТЬ: если она указана и тема зависит от неё (оплата, надбавки, требования, СИЗ, нормы,
  доступы) — найди во фрагментах данные ИМЕННО для этой должности и пиши про неё. Если есть
  только чужие должности — не подставляй их цифры, отметь это в hr_note.
- Пример (подэтап про зарплату): не «доход состоит из нескольких частей», а конкретно —
  оклад/тариф с суммой, из чего складывается переменная часть, размеры и условия премий,
  надбавки и коэффициенты, основания снижения — всё, что есть во фрагментах, с числами.

Правила содержания:
- Опирайся ТОЛЬКО на предоставленные фрагменты внутренних документов компании.
- Не выдумывай цифры, сроки, нормы и названия. Нет данных по теме — ОДНОЙ короткой фразой
  отметь пробел (hr_note «Уточнить у HR: <что именно>»), НЕ раздувай ответ общими фразами.
- Точность чисел: суммы, проценты, сроки, нормы, названия документов — приводи ТОЧНО как в
  документе, не округляй и не искажай. Но приводи только те, что относятся к теме и нужны сейчас.

ФОРМАТ И КРАТКОСТЬ (главное — это НЕ пересказ документа, а короткое человеческое сообщение):
- Соблюдай структуру и объём из блока «В фокусе» (число блоков/пунктов, тон). Если он задаёт
  «5–7 блоков, 1–2 предложения на блок» — не превышай. По умолчанию: коротко, без «простыни».
- Один пункт = 1–2 предложения. Не дублируй один смысл дважды: body и key_points не должны
  повторять друг друга (абзац + те же маркеры = брак), outro не повторяет intro.
- Не тащи юридическую обвязку: оглавление, стороны и сферу действия договора, комиссии,
  «порядок применения» ЛНА — если это прямо не тема подэтапа.
- Не смешивай документы разных юрлиц/компаний в одном ответе — бери актуальный ЛНА площадки
  сотрудника. Несколько версий одного документа — не склеивай, возьми одну актуальную.
- Детали, точные суммы и редкие/узкие условия, не критичные прямо сейчас, не выгружай в тело —
  вынеси в hr_note «подробности — по запросу/позже», а не растягивай сообщение.
- Где уместно (льготы, забота) показывай ВЫГОДУ сотруднику и участие компании
  («компенсируем/организуем/предоставляем/подключаем»), а не сухие условия и отсылки.
- Обвязка (intro/outro) — дружелюбная, но КРАТКАЯ (макс. 1 фраза). Вся суть — в body/key_points.
- НЕ ссылайся на источники: не называй документы и файлы, по которым написано сообщение,
  без «согласно положению…», «в регламенте указано…», номеров пунктов и разделов. Пиши сами
  факты. (Документы, которые сотруднику нужно ПРИНЕСТИ или ПОДПИСАТЬ, — называть можно.)
- НЕ описывай устройство системы: как приходят сообщения и по какому расписанию, дни и этапы
  программы, шаблоны и поля, личный кабинет, приложение, вкладки и кнопки.
- Обращайся на «вы», имя — плейсхолдер [Имя]; другие неизвестные — [ФИО наставника] и т.п.
- Пиши по-русски. Заполняй только уместные поля (см. тип сообщения); ненужные —
  пустой строкой/массивом. Никакого текста вне JSON.
"""


def _build_unified_content(data: dict, substage: dict) -> dict:
    """Нормализует ответ LLM в унифицированный envelope + готовит text и converted."""
    import msgconvert
    kind = substage.get("kind") or "message"
    content = {
        "format": "unified/1",
        "kind": kind,
        "title": str(data.get("title") or substage.get("title") or "").strip(),
        "intro": str(data.get("intro") or "").strip(),
        "body": str(data.get("body") or "").strip(),
        "key_points": [str(p).strip() for p in (data.get("key_points") or []) if str(p).strip()],
        "questions": data.get("questions") or [],
        "checklist": [str(p).strip() for p in (data.get("checklist") or []) if str(p).strip()],
        "outro": str(data.get("outro") or "").strip(),
        "hr_note": str(data.get("hr_note") or "").strip(),
    }
    content["text"] = msgconvert.to_text(content)          # плоский текст (совместимость)
    content["converted"] = msgconvert.convert(content, kind)  # формат под тип подэтапа
    return content


def _substage_query(stage: dict, substage: dict) -> str:
    parts = [stage.get("title", ""), substage.get("title", ""), substage.get("brief", "")]
    parts += substage.get("tags") or []
    return ". ".join(p for p in parts if p)


def pick_topics(stage: dict, substage: dict, topic_list: list) -> list:
    """
    LLM выбирает смысловые папки базы знаний под подэтап. Возвращает slug'и папок.
    Фолбэк — все включённые папки (поиск без сужения). topic_list — список папок
    (folders.list_folders): slug, name, description, criteria.
    """
    from rag import small_llm, parse_json_response

    available = list(topic_list or [])
    if not available:
        return []
    slugs = {t["slug"].casefold(): t["slug"] for t in available}

    topic_lines = "\n".join(
        f"- {t['slug']} — {t.get('name', '')}: {t.get('description', '')} "
        f"[{'; '.join(t.get('criteria') or [])}]"
        for t in available
    )
    user = (
        f"Темы базы знаний:\n{topic_lines}\n\n"
        f"Этап: {stage.get('title')}\n"
        f"Подэтап: {substage.get('title')} (тип: {substage.get('kind')})\n"
        f"Что должен написать бот: {substage.get('brief')}\n"
        f"Ключевые слова подэтапа: {', '.join(substage.get('tags') or []) or '—'}"
    )

    try:
        data = parse_json_response(small_llm(PICK_TOPICS_SYSTEM, user, step_name="PICK_TOPICS"))
        picked = [slugs[str(s).casefold()] for s in (data.get("topics") or [])
                  if str(s).casefold() in slugs]
    except Exception:
        picked = []

    return picked or [t["slug"] for t in available]


def substages_with_docs(filenames=None) -> set:
    """id подэтапов каталога, к которым по классификации docpipe («вкладка папки»)
    отнесён хотя бы один документ (LLM-разметка блоков, не косинус). Пусто -> нет
    привязок (генерировать нечего, всё пойдёт в «пропущено»)."""
    try:
        import docpipe
        _, docs = docpipe.document_assignments(filenames=filenames)
    except Exception as e:
        print(f"[gen] привязка документов недоступна: {e}")
        return set()
    out = set()
    for d in docs:
        for s in d.get("substages") or []:
            if s.get("substage_id"):
                out.add(s["substage_id"])
    return out


def plan_coverage(plan: dict, present: set) -> dict:
    """Покрытие плана документами: подэтап обеспечен, если по его теме (_doc_keys) размечен
    хоть один документ. missing — чего не хватает, сгруппировано по этапам."""
    total = covered = 0
    missing = []
    for stage in plan.get("stages") or []:
        gaps = []
        for sub in stage.get("substages") or []:
            total += 1
            if any(k in present for k in _doc_keys(stage, sub)):
                covered += 1
            else:
                gaps.append(sub.get("title") or "")
        if gaps:
            missing.append({"stage": stage.get("title") or "", "substages": gaps})
    return {"plan_id": plan.get("plan_id"), "title": plan.get("title"),
            "total": total, "covered": covered, "missing": missing}


def plan_board(plan: dict, docs: list) -> tuple:
    """Доска «этапы ↔ документы» в разрезе конкретного плана (свой план тоже): этапы и подэтапы
    плана, документы подэтапа — размеченные по его темам (_doc_keys). Вход и выход — в формате
    documents.build_board (docs — из docpipe.document_assignments по каталогу)."""
    stages, owners = [], {}                      # тема каталога -> [(этап плана, подэтап плана)]
    for st in plan.get("stages") or []:
        stages.append({"id": st["id"], "title": st.get("title") or "", "description": st.get("description") or "",
                       "substages": [{"id": s["id"], "title": s.get("title") or ""} for s in st.get("substages") or []]})
        for sub in st.get("substages") or []:
            for k in _doc_keys(st, sub):
                owners.setdefault(k, []).append((st["id"], sub["id"]))
    out = []
    for d in docs:
        subs = [{"stage_id": sid, "substage_id": pid, "score": a.get("score")}
                for a in d.get("substages") or [] for sid, pid in owners.get(a.get("substage_id"), [])]
        out.append({**d, "substages": subs, "stage_ids": sorted({a["stage_id"] for a in subs})})
    return stages, out


NO_DOC_REASON ="Нет документа, отнесённого к этому подэтапу — загрузите документ и запустите догенерацию"


# Версия промптов генерации. Входит в отпечаток сообщения: поменяли GENERATE_SYSTEM /
# KIND_GEN_HINT так, что старые тексты надо переписать, — увеличьте, иначе не трогайте.
GEN_PROMPT_VERSION = "3"   # 2: без ссылок на документы; 3: key_points без пересказа body
KEEP_STATUSES = ("generated", "edited")   # готовые тексты: без изменений входов не трогаем


def _doc_keys(stage: dict, substage: dict) -> list:
    """Темы каталога «<этап>.<подэтап>», документами которых питается подэтап: у подэтапа из
    каталога — его собственная, у своего — подобранные при сохранении (assign_topics)."""
    stage_cat, sub_cat = stage.get("catalog_id"), substage.get("catalog_id")
    if stage_cat and sub_cat:
        return [f"{stage_cat}.{sub_cat}"]
    return [k for k in (substage.get("topic_keys") or []) if k]


def _context_for(keys: list, cache: Optional[dict] = None) -> tuple:
    """Блоки документов тем keys под бюджет символов -> (ctx_parts, sources). cache — общий
    на пакет {key: blocks}, чтобы 30 должностей не делали 30 одинаковых запросов к БД."""
    import docpipe
    seen_src, seen_txt, budget, ctx_parts = [], set(), CONTEXT_CHAR_BUDGET, []
    for key in keys:
        if cache is not None and key in cache:
            blocks = cache[key]
        else:
            blocks = docpipe.blocks_for_substage(key)
            if cache is not None:
                cache[key] = blocks
        for c in blocks:
            piece = c["text"]
            if piece in seen_txt:
                continue
            if budget - len(piece) < 0 and ctx_parts:
                return ctx_parts, seen_src
            seen_txt.add(piece)
            budget -= len(piece)
            ctx_parts.append(f"--- Блок (документ: {c['source']}) ---\n{piece}")
            if c["source"] and c["source"] not in seen_src:
                seen_src.append(c["source"])
    return ctx_parts, seen_src


def message_fingerprint(stage: dict, substage: dict, position: str, ctx_parts: list) -> str:
    """SHA-256 всего, что уходит в модель по подэтапу: версия промпта, этап/подэтап/тип/бриф,
    должность и сами фрагменты документов. Совпал с сохранённым — текст актуален, модель не
    зовём. Изменился документ, бриф или должность — меняется отпечаток только этих подэтапов."""
    return _sha([GEN_PROMPT_VERSION, stage.get("title"), substage.get("title"), substage.get("kind"),
                 substage.get("brief"), substage.get("tags") or [], (position or "").strip(),
                 _doc_keys(stage, substage), ctx_parts])


def _reuse(prev: dict, fp: str) -> dict:
    return {"content": prev.get("content", {}), "topics_used": prev.get("topics_used", []),
            "sources": prev.get("sources", []), "status": prev.get("status"),
            "error": prev.get("error"), "actions": prev.get("actions", []),
            "fingerprint": fp, "reused": True}


def prepare_substage(stage: dict, substage: dict, position: str = "", docs_present=None,
                     prev: Optional[dict] = None, require_document: bool = True,
                     cache: Optional[dict] = None) -> tuple:
    """Всё, что можно решить БЕЗ модели. Возвращает (payload, prompt):
    prompt=None — модель не нужна (пропуск без документа или текст уже актуален), payload готов;
    иначе payload — заготовка (источники, отпечаток), а prompt надо отправить в модель."""
    result = {"topics_used": [], "sources": [], "status": "generated", "error": None,
              "content": {"format": "unified/1", "text": ""}}
    keys = _doc_keys(stage, substage)
    if require_document:
        present = docs_present if docs_present is not None else substages_with_docs()
        if not any(k in present for k in keys):
            result.update(status="skipped", error=NO_DOC_REASON, fingerprint=None)
            return result, None

    ctx_parts, sources = _context_for(keys, cache)
    fp = message_fingerprint(stage, substage, position, ctx_parts)
    result["fingerprint"] = fp
    result["sources"] = [{"source": s, "folders": [], "page": None, "score": 1.0} for s in sources]
    if prev and prev.get("status") in KEEP_STATUSES:
        # Ручную правку не перезатираем пакетной генерацией. Тексты, сгенерированные до
        # появления отпечатков (нет fingerprint), считаем актуальными — иначе первый же
        # запуск после обновления заново оплатил бы весь план.
        if prev.get("status") == "edited" or prev.get("fingerprint") in (None, fp):
            return _reuse(prev, fp), None

    if not ctx_parts:
        result.update(status="skipped",
                      error="Документ отнесён к подэтапу, но размеченных фрагментов не найдено")
        return result, None

    kind = substage.get("kind") or "message"
    pos_line = f"Должность сотрудника: {position}\n" if (position or "").strip() else ""
    focus_line = _focus_block(stage.get("catalog_id"), substage.get("catalog_id"))
    prompt = (
        f"Этап программы адаптации: {stage.get('title')}\n"
        f"Подэтап: {substage.get('title')}\n"
        f"{pos_line}"
        f"Тип сообщения: {kind}. {KIND_GEN_HINT.get(kind, KIND_GEN_HINT['message'])}\n\n"
        f"Что должен написать бот:\n{substage.get('brief') or substage.get('title')}\n"
        f"{focus_line}\n"
        f"Фрагменты внутренних документов компании:\n" + "\n\n".join(ctx_parts)
    )
    return result, prompt


def generate_substage_message(stage: dict, substage: dict, topic_list: list, position: str = "",
                              docs_present: "set | None" = None, require_document: bool = True,
                              prev: Optional[dict] = None, cache: Optional[dict] = None) -> dict:
    """Текст одного подэтапа. Ошибки не поднимает — возвращает status.
    Без документа по теме подэтапа — status='skipped', модель не зовём. prev — сохранённое
    сообщение: если отпечаток входов не изменился, возвращаем его как есть (reused=True)."""
    try:
        result, prompt = prepare_substage(stage, substage, position, docs_present, prev,
                                          require_document, cache)
        if prompt is None:
            return result
        from rag import big_llm, parse_json_response
        raw = big_llm(GENERATE_SYSTEM, prompt)
        try:
            data = parse_json_response(raw)
        except Exception:
            data = {"body": (raw or "").strip()}     # фолбэк: непарсибельный ответ -> в body
        result["content"] = _build_unified_content(data, substage)
        return result
    except Exception as e:
        return {"topics_used": [], "sources": [], "status": "error", "error": str(e),
                "content": {"format": "unified/1", "text": ""}}


def build_schedule(plan: dict, generated: dict, profession: str = "") -> dict:
    """
    Собирает финальный документ: плоский список сообщений с временем отправки.
    generated — {message_id: результат generate_substage_message}.
    profession — должность, под которую собран контент ("" = общее расписание).
    """
    items = resolve_schedule(plan)
    messages = []
    for item in items:
        payload = generated.get(item["message_id"], {})
        messages.append({
            **item,
            "content": payload.get("content", {"format": "markdown", "text": ""}),
            "topics_used": payload.get("topics_used", []),
            "sources": payload.get("sources", []),
            "status": payload.get("status", "pending"),
            "error": payload.get("error"),
            "fingerprint": payload.get("fingerprint"),
            # Зарезервировано под мессенджеры: кнопки/варианты ответа сотрудника
            "actions": payload.get("actions", []),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan.get("plan_id"),
        "plan_title": plan.get("title"),
        "role": plan.get("role"),
        "profession": profession,
        "start_date": plan.get("start_date"),
        "timezone": plan.get("timezone", DEFAULT_TIMEZONE),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }


def save_schedule(plan_id: str, schedule: dict, profession: str = ""):
    """Сохраняет расписание в БД (plan_schedules, JSONB). Пустая profession -> общее расписание;
    иначе — под конкретную должность. Так один план хранит и общий контент, и вариант под профессию."""
    prof = (profession or "").strip()
    db.execute(
        "INSERT INTO plan_schedules (plan_id, profession, data, updated_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (plan_id, profession) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
        (plan_id, prof, Json(schedule), datetime.now().isoformat(timespec="seconds")),
    )


def delete_schedule(plan_id: str, profession: str) -> bool:
    """Удаляет расписание под конкретную должность (тексты плана). Общее (profession='')
    не трогаем — его удалять нечем осмысленным. Возвращает True, если строка удалена."""
    prof = (profession or "").strip()
    if not prof:
        return False
    existed = db.query("SELECT 1 FROM plan_schedules WHERE plan_id = %s AND profession = %s",
                       (plan_id, prof), fetch="one") is not None
    db.execute("DELETE FROM plan_schedules WHERE plan_id = %s AND profession = %s", (plan_id, prof))
    return existed


# ---------- Фоновые задачи генерации ----------
# Состояние задач — в общем jobstore (Redis при мультипроцессе, иначе in-memory),
# чтобы прогресс и «Отмена» были видны между web-воркером и worker-процессом.
import jobstore

_JOB_NS = "gen"


def _set_job(job_id: str, **fields):
    return jobstore.set_job(_JOB_NS, job_id, **fields)


def get_job(job_id: str) -> Optional[dict]:
    return jobstore.get_job(_JOB_NS, job_id)


def cancel_job(job_id: str) -> Optional[dict]:
    """Помечает задачу генерации на отмену. Цикл увидит флаг перед следующим
    подэтапом, сохранит уже сгенерированное и завершится статусом 'cancelled'."""
    return jobstore.cancel_job(_JOB_NS, job_id)


def _norm_profs(positions: Optional[list], include_general: bool) -> list:
    """Уникальные должности + общее ("") как фолбэк для профессий без своего расписания."""
    profs = []
    for p in (positions or []):
        p = (p or "").strip()
        if p and p not in profs:
            profs.append(p)
    profs = profs or [""]                 # хотя бы общее расписание
    if include_general and profs != [""] and "" not in profs:
        profs = profs + [""]              # плюс общее — для не перечисленных должностей
    return profs


def _substage_of(plan: dict, item: dict) -> tuple:
    stage = next((s for s in plan.get("stages") or [] if s["id"] == item["stage"]["id"]), {})
    sub = next((s for s in stage.get("substages") or [] if s["id"] == item["substage"]["id"]),
               item["substage"])
    return stage, sub


def estimate_generation(plan: dict, profs: list) -> dict:
    """Сколько запросов к модели реально понадобится — без единого вызова модели: сверка
    отпечатков входов с сохранёнными текстами. llm=0 -> «всё актуально»."""
    items = resolve_schedule(plan)
    docs_present = substages_with_docs()
    cache, llm, skipped, fresh = {}, 0, 0, 0
    for prof in profs:
        sch = _load_exact(plan["plan_id"], prof) or {}
        existing = {m.get("message_id"): m for m in (sch.get("messages") or [])}
        for item in items:
            stage, sub = _substage_of(plan, item)
            payload, prompt = prepare_substage(stage, sub, prof, docs_present,
                                               existing.get(item["message_id"]), cache=cache)
            if prompt is not None:
                llm += 1
            elif payload.get("status") == "skipped":
                skipped += 1
            else:
                fresh += 1
    return {"llm_calls": llm, "up_to_date": fresh, "skipped": skipped,
            "total": len(items) * len(profs), "professions": len(profs)}


def _load_exact(plan_id: str, profession: str) -> Optional[dict]:
    row = db.query("SELECT data FROM plan_schedules WHERE plan_id = %s AND profession = %s",
                   (plan_id, profession), fetch="one")
    return row["data"] if row else None


def running_job_for(plan_id: str) -> Optional[dict]:
    """Идущая генерация этого плана (защита от повторных нажатий и параллельных запусков)."""
    lock = jobstore.get_job("genplan", plan_id) or {}
    job = get_job(lock.get("job_id") or "") if lock.get("job_id") else None
    return job if job and job.get("status") in ("queued", "running") else None


def start_generation(plan: dict, positions: Optional[list] = None, include_general: bool = True,
                     only_missing: bool = False) -> dict:
    """Запускает генерацию плана в фоне. positions — должности: под КАЖДУЮ своё расписание,
    плюс общее (profession="") при include_general.

    Генерация всегда ИНКРЕМЕНТАЛЬНАЯ: модель зовётся только для подэтапов, у которых
    изменились входы (документы, бриф, должность) или текста ещё нет. Ничего не менялось —
    задача не создаётся, ответ status='up_to_date'. Пока идёт генерация плана, новая не
    стартует: возвращается текущая (already_running). only_missing оставлен для совместимости
    — «догенерация» теперь частный случай инкрементальной генерации."""
    profs = _norm_profs(positions, include_general)
    running = running_job_for(plan["plan_id"])
    if running:
        return {**running, "already_running": True}
    # Два нажатия подряд (или два админа) — оба видели «ничего не идёт», пока считалась
    # оценка. Замок на время запуска: второй получает busy, а не вторую платную генерацию.
    if not jobstore.claim("genstart", plan["plan_id"], ttl=300):
        running = running_job_for(plan["plan_id"])
        return {**running, "already_running": True} if running else {"status": "busy", "job_id": None}
    try:
        est = estimate_generation(plan, profs)
        if est["llm_calls"] == 0:
            return {"status": "up_to_date", "job_id": None, **est}

        job_id = str(uuid.uuid4())
        _set_job(job_id, plan_id=plan["plan_id"], status="queued", total=est["total"], done=0,
                 current=None, started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), finished_at=None,
                 errors=0, skipped=0, reused=0, llm_calls=0, llm_planned=est["llm_calls"], error=None,
                 professions=len(profs))
        jobstore.set_job("genplan", plan["plan_id"], job_id=job_id, dirty=False)
        # Тяжёлую генерацию — в очередь: worker-процесс (RQ) при Redis, иначе daemon-поток.
        import jobs
        jobs.enqueue_generation(job_id, plan, profs, only_missing)
        return get_job(job_id)
    finally:
        jobstore.release("genstart", plan["plan_id"])


def refresh_generated_plans() -> int:
    """Документы изменились -> догенерировать/обновить тексты у планов, где они уже есть.
    Благодаря отпечаткам модель трогает только подэтапы, чьи документы поменялись. Если
    генерация плана уже идёт — помечаем dirty, она перезапустится по окончании. -> число запусков."""
    started = 0
    for p in list_plans():
        if not p.get("generated"):
            continue                       # тексты ни разу не генерировали — без спроса не тратим
        pid = p["plan_id"]
        if running_job_for(pid):
            jobstore.set_job("genplan", pid, dirty=True)
            continue
        plan = load_plan(pid)
        if not plan:
            continue
        profs = [x["profession"] for x in list_schedule_professions(pid)]
        job = start_generation(plan, positions=profs, include_general=True)
        started += 1 if job.get("job_id") else 0
    return started


def _refresh_inboxes(plan_id: str):
    """Тексты плана поменялись -> ещё не доставленные сообщения сотрудников пересобираются
    (иначе заведённые до генерации получали бы старые/пустые тексты). Сбой не критичен."""
    try:
        import messaging
        messaging.refresh_plan(plan_id)
    except Exception as e:
        print(f"[gen] инбоксы сотрудников не обновлены: {e}")


def _run_generation(job_id: str, plan: dict, profs: list, only_missing: bool = False):
    """Тело генерации плана — выполняется в worker-процессе (RQ) или потоке-фолбэке.
    Прогресс и отмена идут через общий jobstore (_set_job/get_job). Для каждого подэтапа
    сверяется отпечаток входов с сохранённым текстом — модель зовётся только при изменении."""
    import folders
    items = resolve_schedule(plan)
    _set_job(job_id, status="running")
    errors = skipped = done = reused = calls = 0
    try:
        topic_list = folders.list_folders(include_disabled=False)
        docs_present = substages_with_docs()   # какие подэтапы обеспечены документом
        cache = {}
        for prof in profs:
            sch = _load_exact(plan["plan_id"], prof) or {}
            existing = {m.get("message_id"): m for m in (sch.get("messages") or [])}
            generated = {}
            label = prof or "общее"
            cancelled = False

            def _gen_one(item):
                stage, substage = _substage_of(plan, item)
                return item["message_id"], item, generate_substage_message(
                    stage, substage, topic_list, position=prof, docs_present=docs_present,
                    prev=existing.get(item["message_id"]), cache=cache)

            # Параллельная генерация подэтапов (каждый — вызов DeepSeek, I/O-bound).
            _lock = threading.Lock()
            with ThreadPoolExecutor(max_workers=_GEN_WORKERS) as ex:
                futs = [ex.submit(_gen_one, it) for it in items]
                for fut in as_completed(futs):
                    if (get_job(job_id) or {}).get("cancel"):
                        cancelled = True
                        ex.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        mid, item, payload = fut.result()
                    except Exception:
                        with _lock:
                            errors += 1; done += 1
                            _set_job(job_id, done=done, errors=errors)
                        continue
                    with _lock:
                        if payload.get("reused"):
                            reused += 1
                        elif payload["status"] == "error":
                            errors += 1
                        elif payload["status"] == "skipped":
                            skipped += 1
                        else:
                            calls += 1
                        generated[mid] = payload
                        done += 1
                        _set_job(job_id, done=done, errors=errors, skipped=skipped, reused=reused,
                                 llm_calls=calls,
                                 current=f"[{label}] {item['stage']['title']} → {item['substage']['title']}")
            # Отмена или сбой подэтапа: готовое не теряем — не пересчитанные подэтапы
            # оставляем прежними (раньше при сбое одного вызова его текст стирался).
            for mid, prev in existing.items():
                generated.setdefault(mid, {**prev, "reused": True})
            if generated:   # сохраняем, что успели (частичное расписание не теряем)
                save_schedule(plan["plan_id"], build_schedule(plan, generated, profession=prof), profession=prof)
            if cancelled:
                _refresh_inboxes(plan["plan_id"])
                _set_job(job_id, status="cancelled", current=None,
                         finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                return
        _refresh_inboxes(plan["plan_id"])
        _set_job(job_id, status="done", current=None,
                 finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    except Exception as e:
        _set_job(job_id, status="error", error=str(e),
                 finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    # Пока шла генерация, пришли новые документы -> один повторный инкрементальный прогон.
    lock = jobstore.get_job("genplan", plan["plan_id"]) or {}
    if lock.get("job_id") == job_id and lock.get("dirty"):
        jobstore.set_job("genplan", plan["plan_id"], dirty=False)
        fresh = load_plan(plan["plan_id"])
        if fresh:
            start_generation(fresh, positions=profs, include_general=False)


def regenerate_one(plan: dict, message_id: str, profession: str = "") -> Optional[dict]:
    """
    Перегенерирует один подэтап и обновляет сохранённое расписание нужной профессии.
    Возвращает обновлённое сообщение или None, если подэтап не найден.
    """
    import folders

    items = {i["message_id"]: i for i in resolve_schedule(plan)}
    item = items.get(message_id)
    if not item:
        return None

    stage = next((s for s in plan.get("stages") or [] if s["id"] == item["stage"]["id"]), {})
    substage = next((s for s in stage.get("substages", []) if s["id"] == item["substage"]["id"]), None)
    if substage is None:
        return None

    payload = generate_substage_message(stage, substage, folders.list_folders(include_disabled=False),
                                        position=profession)

    schedule = load_schedule(plan["plan_id"], profession)
    if schedule is None:
        schedule = build_schedule(plan, {message_id: payload}, profession=profession)
    else:
        for index, msg in enumerate(schedule.get("messages") or []):
            if msg.get("message_id") == message_id:
                schedule["messages"][index] = {**item, **payload,
                                               "actions": msg.get("actions", [])}
                break
        else:
            schedule.setdefault("messages", []).append({**item, **payload, "actions": []})
        schedule["generated_at"] = datetime.now().isoformat(timespec="seconds")

    save_schedule(plan["plan_id"], schedule, profession=profession)
    _refresh_inboxes(plan["plan_id"])
    return next((m for m in schedule["messages"] if m["message_id"] == message_id), None)


def rerender_saved_texts() -> int:
    """Пересобрать тексты уже сохранённых сообщений по текущим правилам msgconvert (без
    повторов абзаца списком, без служебной hr_note) — без обращения к модели. Запускать
    после изменения сборки текста. -> число обновлённых сообщений."""
    import msgconvert
    changed = 0
    for p in list_plans():
        pid, touched = p["plan_id"], False
        for prof in [""] + [x["profession"] for x in list_schedule_professions(pid)]:
            sch = _load_exact(pid, prof)
            if not sch:
                continue
            dirty = False
            for msg in sch.get("messages") or []:
                content = msg.get("content") or {}
                if content.get("format") != "unified/1":
                    continue
                if msg.get("status") == "edited":
                    content["key_points"] = []          # правка админа — это весь текст целиком
                kind = (msg.get("substage") or {}).get("kind") or content.get("kind") or "message"
                text, conv = msgconvert.to_text(content), msgconvert.convert(content, kind)
                if text != content.get("text") or conv != content.get("converted"):
                    content["text"], content["converted"] = text, conv
                    msg["content"] = content
                    dirty = True
                    changed += 1
            if dirty:
                save_schedule(pid, sch, profession=prof)
                touched = True
        if touched:
            _refresh_inboxes(pid)
    return changed


def edit_message_text(plan_id: str, message_id: str, text: str, profession: str = "") -> Optional[dict]:
    """Ручная правка текста одного сообщения в расписании нужной профессии.
    Возвращает обновлённое сообщение или None, если сообщения нет."""
    schedule = load_schedule(plan_id, profession)
    if schedule is None:
        return None
    import msgconvert
    for msg in schedule.get("messages") or []:
        if msg.get("message_id") == message_id:
            # Ручная правка задаёт плоский текст: кладём его в body и пересобираем
            # унифицированный envelope (text + converted под тип подэтапа).
            content = dict(msg.get("content") or {})
            content["format"] = "unified/1"
            content["body"] = text
            content["intro"] = ""
            content["outro"] = ""
            content["key_points"] = []   # админ правил весь текст целиком — старые пункты не дописываем
            content["text"] = text
            kind = (msg.get("substage") or {}).get("kind") or content.get("kind") or "message"
            content["kind"] = kind
            content["converted"] = msgconvert.convert(content, kind)
            msg["content"] = content
            msg["status"] = "edited"
            msg["error"] = None
            schedule["generated_at"] = datetime.now().isoformat(timespec="seconds")
            save_schedule(plan_id, schedule, profession=profession)
            _refresh_inboxes(plan_id)
            return msg
    return None
