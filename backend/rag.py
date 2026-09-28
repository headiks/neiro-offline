import os
import json
import re
import time

import deepseek

# ---------- Конфигурация ----------
# Онлайн-модель DeepSeek заменила оффлайн qwen. «Малая» и «большая» роли теперь —
# одна и та же быстрая модель deepseek-chat (переопределяется через env).
SMALL_MODEL = os.environ.get("DEEPSEEK_SMALL_MODEL", "") or None
BIG_MODEL = os.environ.get("DEEPSEEK_BIG_MODEL", "") or None
CONFIDENCE_THRESHOLD = 0.55
# Сколько фрагментов уходит в генерацию. Для «списочных» вопросов (перечень состояний,
# набор требований) ответ разбросан по нескольким чанкам — при 3 фрагментах модель видит
# только отсылку («см. приложение»), а не сам список. Даём больше.
MAX_CONTEXT_FRAGMENTS = int(os.environ.get("NEIROMASTER_MAX_CONTEXT_FRAGMENTS", "8"))
HISTORY_WINDOW = 3   # сколько последних вопросов пользователя учитывать при разрешении контекста
# Подробный лог пайплайна: промпты, сырые ответы моделей, тексты вопросов. По умолчанию
# ВЫКЛ — в проде он шумный и может писать в логи чувствительные данные (содержимое
# регламентов, вопросы сотрудников). Включается переменной NEIROMASTER_DEBUG=1.
DEBUG = os.environ.get("NEIROMASTER_DEBUG", "").lower() in ("1", "true", "yes")

# qwen3:14b с ctx 16k под очередью (генерация плана — десятки подэтапов подряд) нередко
# отвечает дольше 90 с — прежний жёсткий таймаут ронял подэтапы в error (Read timed out).
# Держим щедрый дефолт + переопределение через env; на таймаут — один повтор.
SMALL_LLM_TIMEOUT = int(os.environ.get("NEIROMASTER_SMALL_LLM_TIMEOUT", "120"))
BIG_LLM_TIMEOUT = int(os.environ.get("NEIROMASTER_BIG_LLM_TIMEOUT", "300"))

# ---------- Быстрый префильтр для общих фраз и ключевых слов ----------
GREETING_PHRASES = [
    "привет", "здравствуй", "здравствуйте", "добрый день",
    "доброе утро", "добрый вечер", "как дела", "как жизнь",
    "спасибо", "благодарю", "ок", "хорошо", "понял", "да", "нет"
]

# Ключевые слова, которые однозначно указывают на RAG-запрос
RAG_KEYWORDS = [
    "отпуск", "высота", "инструктаж", "техника безопасности", "охрана труда",
    "смена", "регламент", "правила", "норма", "требование", "обязан",
    "положено", "разрешается", "запрещается", "инструкция", "порядок",
    # первая помощь как ТЕМА (информационный вопрос) — не ЧС; острые ЧС ловит detect_emergency
    "первая помощь", "первой помощи", "первую помощь", "оказани",
]

def is_greeting_or_general(text):
    text_lower = text.lower().strip()
    if '?' in text_lower:
        return False
    question_starters = ["что", "как", "где", "когда", "почему", "зачем", "сколько", "кто", "какой"]
    if any(text_lower.startswith(w) for w in question_starters):
        return False
    words = re.findall(r'\w+', text_lower)
    if not words or len(words) > 4:
        return False

    # Многословное приветствие как самостоятельная фраза («как дела», «добрый день»).
    for phrase in GREETING_PHRASES:
        if ' ' in phrase and re.search(r'\b' + re.escape(phrase) + r'\b', text_lower):
            return True

    # Однословные приветствия и подтверждения («да», «нет», «ок», «спасибо») считаем
    # «общей фразой» ТОЛЬКО когда из них состоит ВЕСЬ текст. Иначе «нет» в «мне плохо
    # нет сил» или «да» в «станок сломался да искрит» ошибочно увели бы реальное — и
    # даже тревожное — сообщение в маршрут general мимо RAG и эскалации.
    single = {p for p in GREETING_PHRASES if ' ' not in p}
    if all(w in single for w in words):
        return True
    return False

def has_rag_keywords(text):
    text_lower = text.lower()
    for kw in RAG_KEYWORDS:
        if kw in text_lower:
            return True
    return False

# ---------- Вспомогательные функции ----------
def log(step, msg, data=None):
    if not DEBUG:
        return
    print(f"[{step}] {msg}")
    if data is not None:
        print(f"    {data}")

def small_llm(system, user, step_name="SMALL_LLM"):
    log(step_name, f"Запрос к малой модели:\n  system={system[:80]}...\n  user={user[:80]}...")
    start = time.time()
    # deepseek.chat уже держит повтор при сетевых сбоях и таймауте.
    response = deepseek.chat(system, user, model=SMALL_MODEL, temperature=0,
                             timeout=SMALL_LLM_TIMEOUT)
    log(step_name, f"Ответ получен за {time.time()-start:.3f} сек, длина {len(response)} символов")
    log(step_name, f"Сырой ответ: {response}")
    return response

def big_llm(system, user):
    log("BIG_LLM", f"Запрос к большой модели:\n  system={system[:80]}...\n  user={user[:80]}...")
    start = time.time()
    response = deepseek.chat(system, user, model=BIG_MODEL, temperature=0,
                             timeout=BIG_LLM_TIMEOUT)
    log("BIG_LLM", f"Ответ получен за {time.time()-start:.3f} сек, длина {len(response)} символов")
    log("BIG_LLM", f"Сырой ответ: {response}")
    return response

def parse_json_response(text):
    log("PARSE", f"Попытка извлечь JSON из: {text[:100]}...")
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        raise ValueError("В ответе нет JSON-объекта")
    json_str = text[start:end+1]
    try:
        parsed = json.loads(json_str)
        log("PARSE", f"JSON успешно распарсен: {parsed}")
        return parsed
    except json.JSONDecodeError as e:
        log("PARSE", f"Ошибка парсинга JSON: {e}")
        raise

# ---------- Анализ истории диалога (разрешение контекстных вопросов) ----------
# Если пользователь до этого спрашивал про "работу на высоте" и "какая экипировка нужна",
# а затем задаёт короткий обрывочный вопрос "Где взять" — сам по себе он ни классификатору,
# ни поиску, ни генератору ответа не даст ничего осмысленного. Этот блок смотрит на последние
# HISTORY_WINDOW вопросов пользователя и, если текущий вопрос зависит от контекста,
# переписывает его в полный самостоятельный вопрос ДО того, как он попадёт в classify/search/rerank.
CONTEXTUALIZE_SYSTEM = """
Ты — модуль анализа истории диалога. Твоя задача — понять, ссылается ли текущий вопрос
пользователя на тему предыдущих вопросов, и если да — переписать его в полный самостоятельный вопрос.

Тебе дана история последних вопросов пользователя (от старых к новым) и текущий вопрос.

Правила:
1. Если текущий вопрос уже полный и понятен сам по себе, без истории — верни его БЕЗ ИЗМЕНЕНИЙ
   и "depends_on_context": false.
2. Если текущий вопрос короткий, обрывочный, содержит местоимения ("это", "туда", "он", "их")
   или явно продолжает тему предыдущих вопросов (например "где взять", "а сколько", "почему",
   "а если нет") — перепиши его в полный вопрос, подставив недостающую тему из истории,
   и укажи "depends_on_context": true.
3. Не придумывай фактов, которых не было в истории — только соединяй текущий вопрос с темой
   из истории, не добавляя ничего лишнего.
4. Если история не связана с текущим вопросом по смыслу — верни вопрос без изменений
   и "depends_on_context": false.

Верни ТОЛЬКО JSON: {"standalone_question": "...", "depends_on_context": true/false}. Без пояснений.

Примеры:

История:
- Что нужно для работы на высоте?
- Какие требования к страховочной привязи?
- Какая экипировка нужна для работы на высоте?
Текущий вопрос: "Где взять"
{"standalone_question": "Где взять экипировку для работы на высоте?", "depends_on_context": true}

История:
- Сколько дней отпуска положено?
Текущий вопрос: "А дополнительный?"
{"standalone_question": "Сколько дней дополнительного отпуска положено?", "depends_on_context": true}

История: (пусто или не по теме текущего вопроса)
Текущий вопрос: "Сколько огнетушителей должно быть на складе?"
{"standalone_question": "Сколько огнетушителей должно быть на складе?", "depends_on_context": false}
"""

def resolve_question(question, history):
    """
    history — список последних реплик пользователя вида [{"question": "...", "answer": "..."}, ...]
    от старых к новым (обычно уже обрезан вызывающей стороной до HISTORY_WINDOW).
    """
    if not history:
        log("CONTEXTUALIZE", "История пуста, используем вопрос как есть")
        return {"standalone_question": question, "context_used": False}

    # Явные приветствия/благодарности не нуждаются в переформулировке — не тратим вызов LLM
    if is_greeting_or_general(question):
        log("CONTEXTUALIZE", "Вопрос — приветствие/общая фраза, пропускаем анализ истории")
        return {"standalone_question": question, "context_used": False}

    # Достаточно длинный вопрос с предметными ключевыми словами уже самодостаточен
    if has_rag_keywords(question) and len(question.split()) >= 5:
        log("CONTEXTUALIZE", "Вопрос уже самодостаточен (есть ключевые слова и длина), пропускаем анализ истории")
        return {"standalone_question": question, "context_used": False}

    recent = history[-HISTORY_WINDOW:]
    history_lines = "\n".join(f"- {h['question']}" for h in recent if h.get("question"))
    prompt = f'История последних вопросов пользователя:\n{history_lines}\n\nТекущий вопрос: "{question}"'
    log("CONTEXTUALIZE", f"Анализ вопроса с учётом {len(recent)} предыдущих вопросов")

    raw = small_llm(CONTEXTUALIZE_SYSTEM, prompt, step_name="CONTEXTUALIZE")
    try:
        data = parse_json_response(raw)
        standalone = (data.get("standalone_question") or question).strip() or question
        depends = bool(data.get("depends_on_context", False))
        if depends:
            log("CONTEXTUALIZE", f"Вопрос переформулирован с учётом контекста: '{question}' -> '{standalone}'")
        else:
            log("CONTEXTUALIZE", "Вопрос признан самостоятельным, контекст не использован")
        return {"standalone_question": standalone, "context_used": depends}
    except Exception as e:
        log("CONTEXTUALIZE", f"Ошибка разбора ответа, используем исходный вопрос. Ошибка: {e}")
        return {"standalone_question": question, "context_used": False}

# ---------- ЧС: детерминированные триггеры (ТЗ 1.7) ----------
# Критичный по времени путь простой и предсказуемый: сначала регэкспы, нейронка — только
# вторичная проверка. Совпадение = мгновенный приоритет и выдача инструкции ДО всякого RAG.
# Порядок важен: первое совпадение выигрывает. Инструкция короткая и безопасная; вопрос
# при этом всё равно уходит человеку (эскалация в очередь).
_EMERGENCY_TAIL = ("Немедленно сообщите непосредственному руководителю. "
                   "При угрозе жизни и здоровью звоните 112. Вопрос передан ответственному специалисту.")
EMERGENCY_RULES = [
    # Прямая угроза жизни — высший приоритет. Ловим явные крики о помощи и жизнеугрожающие
    # состояния от первого лица («я умираю», «теряю сознание»). Лучше лишний раз эскалировать.
    (r"умира|умру|помира|погиба|\bспасите\b|\bsos\b|тону|захлеб|истека\w* кровью|"
     r"тер(яю|яет|ять) сознани|мне очень плохо|сейчас умру",
     "угроза жизни",
     "Угроза жизни. Немедленно зовите людей рядом на помощь и звоните 112 (скорая 103). "
     "Если можете — сообщите точно, где вы находитесь."),
    (r"пожар|задымл|возгоран|\bгорит\b|\bогонь\b|полыхает",
     "пожар",
     "Пожар/задымление. Прекратите работу, при возможности обесточьте участок, покиньте помещение по плану эвакуации, не пользуйтесь лифтом."),
    (r"эвакуац",
     "эвакуация",
     "Эвакуация. Двигайтесь к ближайшему выходу по плану эвакуации, помогите тем, кто рядом, не возвращайтесь за вещами."),
    (r"\bвзрыв|обрушен|обвал",
     "авария",
     "Авария (взрыв/обрушение). Отойдите на безопасное расстояние, не приближайтесь к зоне, предупредите окружающих."),
    (r"утечк|разлив|хим(ическ|реагент)|\bгаз(ует|ом|а)?\b|отравлен|токсич",
     "утечка/химия",
     "Утечка/химическая опасность. Покиньте зону, не вдыхайте пары, перекройте источник только если это безопасно."),
    (r"удар(ил|ило|ило меня)? ?ток|электротравм|под напряж|замкнул",
     "электротравма",
     "Электротравма. Обесточьте участок до касания пострадавшего, не трогайте его под напряжением."),
    (r"травм|ранен|\bкровь\b|кровотеч|перелом|ожог|упал с высот|без сознан|потер(ял|яла) сознан|не дыш|задыха|приступ|инфаркт|инсульт|сердц",
     "травма/здоровье",
     "Травма/угроза здоровью. Окажите первую помощь по возможности, не перемещайте пострадавшего без необходимости, вызовите скорую 103/112."),
    (r"напал|ударил|избил|угрож|насил|оружи|захват",
     "угроза/насилие",
     "Угроза безопасности. Отойдите в безопасное место, при угрозе жизни звоните 112."),
]
_EMERGENCY_COMPILED = [(re.compile(p, re.IGNORECASE), t, instr) for p, t, instr in EMERGENCY_RULES]


def detect_emergency(text: str):
    """Первое совпадение триггера ЧС → (risk_type, instruction). Иначе None. Только код,
    без нейронки — предсказуемо и мгновенно."""
    t = text or ""
    for rx, risk_type, instr in _EMERGENCY_COMPILED:
        if rx.search(t):
            return {"risk_type": risk_type, "instruction": instr}
    return None


# ---------- Классификация (усиленная) ----------
CLASSIFY_SYSTEM = """
Ты — модуль маршрутизации. Определи, к какому маршруту отнести вопрос пользователя.

Маршруты:
- "rag" — любые вопросы о правилах, регламентах, инструкциях, нормах, процедурах, условиях работы, льготах, отпусках, технике безопасности, охране труда.
  Вопросы часто начинаются с: что, как, где, когда, почему, зачем, сколько, какой, какие, нужно ли, обязан ли, можно ли, разрешено ли.
- "general" — только короткие приветствия, прощания, благодарности без вопросительного смысла. Пример: "привет", "спасибо", "ок".
- "escalate" — если речь о травме, угрозе, насилии, конфликте, плохом самочувствии (требуется вмешательство человека).

Верни ТОЛЬКО JSON с полями: "route" (одно из значений), "risk_flag" (bool, true только для escalate), "risk_type" (строка или null).

Примеры:
{"route": "rag", "risk_flag": false, "risk_type": null}          # для "Сколько дней отпуска?"
{"route": "rag", "risk_flag": false, "risk_type": null}          # для "Что делать в начале смены?"
{"route": "general", "risk_flag": false, "risk_type": null}      # для "Привет"
{"route": "escalate", "risk_flag": true, "risk_type": "конфликт"} # для "Меня ударили"

Не добавляй пояснений, только JSON.
"""

def route_question(question):
    log("CLASSIFY", f"Классификация вопроса: {question}")
    # Быстрый префильтр для приветствий
    if is_greeting_or_general(question):
        log("CLASSIFY", "Быстрый префильтр: general")
        return {"route": "general", "risk_flag": False, "risk_type": None}
    # Если есть ключевые слова RAG — сразу rag, минуя LLM (экономит время)
    if has_rag_keywords(question):
        log("CLASSIFY", "Быстрый префильтр: найдены ключевые слова RAG, маршрут rag")
        return {"route": "rag", "risk_flag": False, "risk_type": None}

    raw = small_llm(CLASSIFY_SYSTEM, question, step_name="CLASSIFY")
    try:
        data = parse_json_response(raw)
        route = data.get("route", "rag")
        # Настоящие ЧС уже отловлены регэксом detect_emergency ДО классификации. Поэтому
        # "escalate" от малой модели здесь = ложное срабатывание на ИНФОРМАЦИОННОМ вопросе
        # (напр. «при каких состояниях оказывать первую помощь» — это вопрос ПРО правила, а
        # не сообщение о ЧП). Такое отвечаем через RAG; если ответа нет — уйдёт человеку по
        # «нет ответа». Эскалацию решают регэкс ЧС + пустой ответ, а не тематика вопроса.
        if route == "escalate":
            log("CLASSIFY", "escalate от LLM понижен до rag (реальные ЧС ловит регэкс)")
            route = "rag"
        result = {"route": route, "risk_flag": False, "risk_type": None}
        log("CLASSIFY", f"Результат: {result}")
        return result
    except Exception as e:
        log("CLASSIFY", f"Ошибка, возвращаем rag. Ошибка: {e}")
        return {"route": "rag", "risk_flag": False, "risk_type": None}

# Поиск/реранжирование по векторам (Qdrant) удалены: ретрив вопроса идёт по LLM-меткам
# подэтапов (route_substages -> fetch_by_substages, docpipe/Postgres). См. ниже.

# ---------- Генерация ----------
GENERATE_SYSTEM = """
Ты — ассистент по внутренним регламентам завода. Отвечаешь сотруднику ТОЛЬКО по
предоставленному контексту, на русском языке. Задача — дать КОНКРЕТНЫЙ ответ именно на
заданный вопрос, а не общий рассказ по теме.

ШАГ 1. Определи, о чём именно спрашивают: точный предмет вопроса (что, для кого, при каких
условиях). Мысленно раздели контекст на «относится к вопросу» и «не относится», и бери
только первое.

ЧТО БРАТЬ (относится): факты, прямо отвечающие на вопрос — числа, суммы, проценты, сроки,
условия, пороги, шаги, названия документов/должностей, частные случаи и исключения по теме.
ЧТО НЕ БРАТЬ (не относится): вводные преамбулы («настоящее положение разработано…»), общие
цели и миссия, определения не по теме, а также сведения про ДРУГИЕ темы, попавшие в контекст
рядом. Их не пересказывай — это вода.

ДОЛЖНОСТЬ. Если указана должность сотрудника, а вопрос зависит от неё (оплата, надбавки,
требования, СИЗ, нормы, доступы) — найди в контексте данные ИМЕННО для этой должности и
отвечай про неё. Общие для всех правила добавляй, если они применимы. Если в контексте есть
только данные для других должностей — так и скажи, не подставляй чужие цифры.

ФАКТЫ — ПОЛНО И ТОЧНО:
- перечни/суммы/проценты/сроки/условия приводи ДОСЛОВНО и ЦЕЛИКОМ, ничего не округляя;
- из нескольких версий одного перечня бери САМУЮ подробную (нумерованный список), а не фразу-описание;
- ЗАПРЕЩЕНО сокращать перечень словами «и т.д.», «и другие», «и прочее»;
- пример (вопрос про зарплату): не «зарплата состоит из нескольких частей», а конкретно —
  оклад/тариф (сумма), из чего складывается переменная часть, размеры и условия премий,
  надбавки и коэффициенты, основания снижения — всё, что есть в контексте, с числами.

ЗАПРЕЩЕНО отсылать к другому документу/приложению («см. приложение N», «к настоящему
Порядку») — дай сам ответ, а не ссылку.

СТИЛЬ: одна короткая подводка (не обязательна), затем суть по делу. Без воды, без
рассуждений вслух, без повторов. Не растягивай, но не теряй факты.

КАК ГОВОРИТЬ С СОТРУДНИКОМ — отвечай как знающий коллега, а не как программа:
- НЕ упоминай, откуда взяты сведения: никаких «в контексте», «в предоставленных данных /
  фрагментах / материалах», «в документе / регламенте / положении указано», названий
  документов и файлов, номеров пунктов и разделов.
- НЕ рассказывай, как устроена система: рассылка и расписание сообщений, шаблоны, поля,
  дни и этапы программы адаптации, личный кабинет, приложение, ассистент, вкладки и кнопки.
  Если в тексте встречается шаблон или незаполненное поле («Наставник, ФИО», [Имя], «ФИО,
  телефон») — считай, что этих сведений нет, и не пересказывай шаблон.
- Сведений нет — скажи просто: «Точных сведений об этом у меня нет — уточните у <кого по
  теме: мастера участка, наставника, HR>». Не выдумывай фактов.
- Слова «контекст», «материалы», «фрагмент», «документ», «регламент», «шаблон», «данные»,
  «не заполнено», «не указано», «не приводится» в ответе о том, откуда сведения, — ЗАПРЕЩЕНЫ.
  Чего-то нет — одной фразой «точных сведений об этом у меня нет — уточните у …».
- Не привязывай события к «сегодня» или «завтра», если этого нет в вопросе: пиши «в первый
  рабочий день», «в первую неделю».
  Плохо: «В материалах имя наставника не заполнено». «Эти данные не заполнены». «В контексте
  нет данных». «Размер (в процентах не указан)». «Согласно положению о премировании…».
  Хорошо: «Сведений о вашем наставнике у меня нет — уточните у мастера участка или в HR.»
  Вместо «согласно положению, премия…» — сразу сам факт: «Премия …».
"""

# Страховка поверх промпта: детерминированно вырезаем из ответа отсылочные фразы к документу/
# приложению (модель иногда их дописывает вопреки инструкции). Пользователь хочет сам ответ.
_DOC_REF_RE = re.compile(
    r"[^.!?\n]*(?:приведён[а-я]*\s+в\s+приложени|указан[а-я]*\s+в\s+приложени|"
    r"см\.?\s*приложени|в\s+приложении\s*[NN№]|к\s+настоящему\s+Порядку|"
    r"согласно\s+приложени|в\s+соответствии\s+с\s+приложени)[^.!?\n]*[.!?]?",
    re.IGNORECASE)


# Намёки на исходный текст («— эти поля не заполнены», «(размер не указан)»): сотруднику важно,
# что сведений нет и к кому идти, а не то, что где-то лежит незаполненный шаблон.
_NOT_FILLED = r"не\s+(?:указан|заполнен|приведен|приведён)[а-я]*"
_SOURCE_HINT_RES = (
    re.compile(r"\s*\([^()]*?" + _NOT_FILLED + r"[^()]*\)", re.IGNORECASE),
    re.compile(r"\s*[—–,-]\s*(?:эти|это|эта|данные|поля|сведения)\s[^.;—–()\n]*?" + _NOT_FILLED, re.IGNORECASE),
)


def _strip_doc_refs(text: str) -> str:
    out = _DOC_REF_RE.sub("", text or "")
    for rx in _SOURCE_HINT_RES:
        out = rx.sub("", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()

def generate_answer(question, context_fragments, position: str = ""):
    log("GENERATE", f"Генерация ответа с использованием {len(context_fragments)} фрагментов")
    context_text = "\n\n".join([f"--- Фрагмент {i+1} ---\n{frag}" for i, frag in enumerate(context_fragments)])
    pos_line = f"Должность сотрудника: {position}\n" if (position or "").strip() else ""
    user_prompt = f"{pos_line}Вопрос: {question}\n\nКонтекст:\n{context_text}"
    log("GENERATE", f"Сформирован промпт для большой модели:\n{user_prompt}")
    answer = big_llm(GENERATE_SYSTEM, user_prompt)
    answer = _strip_doc_refs(answer)          # убираем отсылки «см. приложение», если проскочили
    log("GENERATE", f"Сгенерированный ответ: {answer}")
    return answer

# ---------- Маршрутизация вопроса по подэтапам (без векторов) ----------
ROUTE_SUBSTAGE_SYSTEM = """Ты — маршрутизатор вопросов нового сотрудника по базе знаний
адаптации. Дан вопрос и список подэтапов (id — тема/описание). Выбери до 3 подэтапов,
к теме которых вопрос ближе всего по СМЫСЛУ. Верни СТРОГО JSON:
{"substages": ["<id>", ...]} — id ТОЛЬКО из списка, самый релевантный первым.
Если вопрос не относится ни к одному — {"substages": []}."""


def _substage_catalog_lines() -> str:
    import planner
    lines = []
    for st in (planner.load_catalog().get("stages") or []):
        for sub in st.get("substage_templates") or []:
            desc = (sub.get("brief") or sub.get("title") or "")[:160]
            lines.append(f"{st['id']}.{sub['id']} — {st.get('title')} / {sub.get('title')}: {desc}")
    return "\n".join(lines)


def route_substages(question, top=3):
    """DeepSeek выбирает топ-N подэтапов, к теме которых вопрос ближе всего (без эмбеддингов)."""
    try:
        raw = small_llm(ROUTE_SUBSTAGE_SYSTEM,
                        f"Вопрос: {question}\n\nПодэтапы:\n{_substage_catalog_lines()}", "ROUTE_SUB")
        data = parse_json_response(raw)
        ids = [s for s in (data.get("substages") or []) if isinstance(s, str) and s.strip()]
        return ids[:top]
    except Exception as e:
        log("ROUTE_SUB", f"не удалось определить подэтапы: {e}")
        return []


def fetch_by_substages(substage_ids, budget=None):
    """Блоки документов, размеченных этими подэтапами (docpipe/Postgres), под бюджет символов."""
    import docpipe
    budget = budget or int(os.environ.get("NEIROMASTER_QA_CONTEXT_CHARS", "24000"))
    frags, sources, seen = [], [], set()
    for sid in substage_ids:
        for c in docpipe.blocks_for_substage(sid):
            t = c["text"]
            if not t or t in seen:
                continue
            if budget - len(t) < 0 and frags:
                return frags, sources
            seen.add(t); budget -= len(t)
            frags.append(t)
            if c["source"] and c["source"] not in sources:
                sources.append(c["source"])
    return frags, sources


# ---------- Основная функция ----------
def handle_question(question, history=None, current_stage_ids=None, position=None):
    """
    history — список предыдущих реплик текущего диалога вида
    [{"question": "...", "answer": "..."}, ...] от старых к новым.
    current_stage_ids — id этапов текущего этапа обучения пользователя (из прогресса);
    используются как приоритет поиска, но не ограничивают его (ТЗ §6).
    position — должность сотрудника из профиля: из двух похожих чанков приоритет получает
    тот, чья профессия ближе к должности (приоритет, не фильтр — общие документы доступны всем).
    """
    log("START", f"Обработка вопроса: {question}")
    total_start = time.time()
    history = (history or [])[-HISTORY_WINDOW:]

    # ЧС (ТЗ 1.7): детерминированные триггеры срабатывают ДО контекстуализации и RAG.
    # Критичный по времени путь не должен зависеть от нейронки. Проверяем исходный текст.
    emergency = detect_emergency(question)
    if emergency:
        log("EMERGENCY", f"Триггер ЧС: {emergency['risk_type']}")
        return {
            "question": question,
            "resolved_question": None,
            "context_used": False,
            "classification": {"route": "escalate", "risk_flag": True, "risk_type": emergency["risk_type"]},
            "route": "escalate",
            "candidates": [],
            "top_fragments": [],
            "answer": f"⚠️ {emergency['instruction']}\n\n{_EMERGENCY_TAIL}",
            "emergency": True,   # app всё равно поставит вопрос в очередь человеку
            "elapsed_time": time.time() - total_start,
            "error": None,
        }

    # Разрешаем зависимость от контекста ДО классификации/поиска — короткие вопросы
    # вроде "Где взять" сами по себе не несут смысла для векторного поиска.
    resolved = resolve_question(question, history)
    effective_question = resolved["standalone_question"]
    context_used = resolved["context_used"]

    # Кэш частых вопросов: тот же по смыслу вопрос (набор значимых слов) при той же базе
    # документов и должности — готовый ответ без маршрутизации и генерации (qacache).
    import qacache
    cached = qacache.get(effective_question, position or "")
    if cached:
        log("CACHE", "Ответ из кэша частых вопросов")
        return {"question": question,
                "resolved_question": effective_question if context_used else None,
                "context_used": context_used, "candidates": [], "top_fragments": [],
                "elapsed_time": time.time() - total_start, "error": None, "cached": True,
                **cached}

    route_info = route_question(effective_question)
    route = route_info["route"]
    log("HANDLE", f"Маршрут: {route}")

    base_result = {
        "question": question,
        "resolved_question": effective_question if context_used else None,
        "context_used": context_used,
        "classification": route_info,
        "route": route,
    }

    if route == "general":
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": "Здравствуйте! Чем я могу вам помочь по вопросам регламентов и охраны труда?",
            "elapsed_time": time.time() - total_start,
            "error": None
        }

    if route != "rag":
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": None,
            "elapsed_time": time.time() - total_start,
            "error": None
        }

    # Новый поиск без векторов: DeepSeek выбирает топ-3 подэтапа по смыслу вопроса,
    # затем берём документы, размеченные этими подэтапами (docpipe/Postgres), и по ним
    # генерируем ответ. Разбивка под контекст модели — в fetch_by_substages.
    picked = route_substages(effective_question)
    log("HANDLE", f"Подэтапы для вопроса: {picked}")
    context_fragments, source_names = fetch_by_substages(picked)
    sources = [{"source": s, "section": None, "page": None} for s in source_names]

    answer = None
    if context_fragments:
        answer = generate_answer(effective_question, context_fragments, position=position or "")
        log("SOURCES", "; ".join(source_names) or "(нет)")
    else:
        log("HANDLE", "Нет размеченных документов под тему вопроса")
    if answer:
        qacache.put(effective_question, position or "",
                    {"answer": answer, "sources": sources, "route": route,
                     "classification": route_info, "route_substages": picked})

    return {
        **base_result,
        "candidates": [],
        "route_substages": picked,
        "top_fragments": context_fragments,
        "sources": sources,
        "answer": answer,
        "elapsed_time": time.time() - total_start,
        "error": None
    }

# ---------- Тест ----------
if __name__ == "__main__":
    # Имитация диалога: три вопроса по теме "работа на высоте", затем обрывочный
    # четвёртый вопрос, который должен быть разрешён через историю.
    dialogue = [
        "Что нужно для работы на высоте?",
        "Какие требования к страховочной привязи?",
        "Какая экипировка нужна для работы на высоте?",
        "Где взять",
    ]
    history = []
    for q in dialogue:
        result = handle_question(q, history=history)
        print(f"\n=== Вопрос: {q!r} ===")
        if result.get("context_used"):
            print(f"    Разрешено как: {result['resolved_question']!r}")
        print(f"    Маршрут: {result['route']}")
        print(f"    Ответ: {result.get('answer')}")
        history.append({"question": q, "answer": result.get("answer")})