"""
pii.py — обезличивание персональных данных перед отправкой текста в DeepSeek.

Всё, что уходит в модель, проходит через deepseek.chat — там текст пользователя
маскируется (Masker.mask), а ответ модели восстанавливается (Masker.unmask). Модель видит
только метки вида [ФИО_1], [ТЕЛ_2], реальные данные не покидают сервер.

Что маскируем (regex + словарь ФИО из data/name_dict, без ML):
  ФИО          «Иванов Иван Иванович» в любом падеже («назначить Петрову Анну Сергеевну»),
               «Иванов И.И.», «И.И. Иванов»;
  телефоны     +7/8 и 10 цифр в любой группировке (мобильные и городские с кодом 3–5 цифр),
               городские «(3452) 56-78-90»;
  email;
  реквизиты    ИНН/КПП/ОГРН/БИК/ОКПО — число после ключевого слова;
  СНИЛС        XXX-XXX-XXX XX;
  счета        20 цифр (р/с, к/с); банковские карты — 16 цифр;
  паспорт      «паспорт 45 12 123456», «серия 4512 номер 123456»;
  дата рождения, домашний адрес (с квартирой), табельный номер;
  организации  ООО/АО/ПАО/ЗАО/ОАО/ИП/ФГУП/МУП «Название» и без кавычек.

ponytail: regex + словарь ловят типовые форматы кадровых документов; одиночное имя без
фамилии («сообщит Ирина») и ФИО латиницей пройдут. Нужна полнота выше — подключить NER
(Natasha/Slovnet) в _find_spans, остальной код не меняется. Выключить: NEIROMASTER_PII_MASK=0.
"""
import json
import os
import re
from pathlib import Path

_NAME_DIR = Path(__file__).resolve().parents[1] / "data" / "name_dict"
_NAME_SET = None


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def name_set() -> set:
    """Имена/отчества/фамилии в нижнем регистре, ё -> е (общий словарь со штаткой)."""
    global _NAME_SET
    if _NAME_SET is None:
        s = set()
        for fn in ("first_names.txt", "middle_names.txt", "last_names.txt"):
            try:
                with open(_NAME_DIR / fn, encoding="utf-8") as f:
                    s.update(_norm(ln.strip()) for ln in f if ln.strip())
            except OSError:
                pass
        _NAME_SET = s
    return _NAME_SET


def enabled() -> bool:
    return os.environ.get("NEIROMASTER_PII_MASK", "1") != "0"


# ---------- Падежи ----------
# Словарь хранит именительный падеж. В приказах и регламентах ФИО чаще в косвенных:
# «назначить Петрову Анну Сергеевну», «поручить Иванову Ивану Ивановичу», «с Сидоровым».
# Пробуем вернуть слово к именительному, заменяя падежное окончание (суффикс -> замена).
_CASE_RULES = (
    ("ому", "ий"), ("ого", "ий"), ("ым", "ий"), ("им", "ий"),          # Троицкому -> Троицкий
    ("ой", "ая"), ("ую", "ая"),                                        # Троицкой -> Троицкая
    ("ой", "а"), ("ей", "а"), ("ую", "а"), ("у", "а"), ("е", "а"), ("ы", "а"), ("и", "а"),  # Анну -> Анна
    ("ей", "я"), ("ю", "я"), ("и", "я"),                               # Марию -> Мария
    ("ем", "й"), ("ю", "й"), ("я", "й"), ("е", "й"),                   # Андрею -> Андрей
    ("ым", ""), ("ом", ""), ("ем", ""), ("у", ""), ("а", ""), ("е", ""),  # Ивановым -> Иванов
)
_PATRONYMIC = re.compile(r"(?:ович|евич|ич|овн|евн|ичн|инич)(?:а|у|ем|ом|е|ой|ы|ну|на|ной|не|ны)?$")


def _name_hit(word: str) -> int:
    """2 — слово есть в словаре как есть, 1 — после снятия падежного окончания, 0 — нет."""
    w = _norm(word)
    names = name_set()
    if w in names:
        return 2
    for suffix, repl in _CASE_RULES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 2 and (w[:-len(suffix)] + repl) in names:
            return 1
    return 0


_W = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_I = r"[А-ЯЁ]\.\s?"
_D = r"[\s\-()]*"
# (метка, regex, номер группы с данными). Порядок важен: длинные/специфичные — раньше.
_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), 0),
    ("ОРГ", re.compile(r"\b(?:ООО|АО|ПАО|ЗАО|ОАО|ИП|НКО|ФГУП|МУП|ГУП)\s*[«\"“][^»\"”\n]{1,80}[»\"”]"), 0),
    ("ОРГ", re.compile(r"\b(?:ООО|АО|ПАО|ЗАО|ОАО|НКО|ФГУП|МУП|ГУП)\s+[А-ЯЁA-Z][\w-]*(?:\s+[А-ЯЁA-Z][\w-]*){0,2}"), 0),
    ("РЕКВ", re.compile(r"(?i)\b(?:ИНН|КПП|ОГРНИП|ОГРН|БИК|ОКПО)\s*[:№]?\s*(\d{8,15})\b"), 1),
    ("СЧЁТ", re.compile(r"(?<!\d)\d{20}(?!\d)"), 0),
    ("КАРТА", re.compile(r"(?<![\d-])\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}(?![\d-])"), 0),
    ("СНИЛС", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)"), 0),
    ("ПАСПОРТ", re.compile(r"(?i)паспорт\w*[^\d\n]{0,30}?(\d{2}\s?\d{2}\s*(?:№|номер|n)?\s*\d{6})(?!\d)"), 1),
    ("ДАТА", re.compile(r"(?i)(?:дата\s+рождения|родил(?:ся|ась)|д\.\s?р\.)\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"), 1),
    ("ДАТА", re.compile(r"(?<![\d.])(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s*г\.?\s*р\.?"), 1),
    ("АДРЕС", re.compile(r"(?i)(?:(?:г\.|город)\s*[А-ЯЁ][\w-]+,?\s*)?(?:ул\.|улица|пр-т|пр\.|проспект|пер\.|переулок|"
                          r"б-р|бульвар|ш\.|шоссе|мкр\.?|микрорайон)\s*[^,\n]{2,40},\s*(?:д\.|дом)\s*\d+[\w/]*"
                          r"(?:,\s*(?:корп\.|к\.|стр\.)\s*\d+\w*)?,\s*(?:кв\.|квартира)\s*\d+"), 0),
    ("ТАБ", re.compile(r"(?i)таб(?:ельн\w*|\.)\s*(?:номер|№|no)?\s*:?\s*(\d{3,10})"), 1),
    # Телефон: +7/8 и ещё 10 цифр в любой группировке — мобильные и городские с кодом 3–5 цифр
    # («8 (3452) 56-78-90»); городской с кодом в скобках без восьмёрки — «(3452) 56-78-90».
    ("ТЕЛ", re.compile(rf"(?<![\d\w])(?:\+7|8){_D}(?:\d{_D}){{9}}\d(?!\d)"), 0),
    ("ТЕЛ", re.compile(r"(?<![\d\w])\(\d{3,5}\)\s*\d{1,3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)"), 0),
    # «Иванов И.И.» / «И.И. Иванов» — два инициала, иначе «Приложение А.» сошло бы за ФИО
    ("ФИО", re.compile(rf"\b{_W}\s{_I}{_I}(?!\w)|(?<!\w){_I}{_I}{_W}\b"), 0),
]
_FIO_RUN = re.compile(rf"\b{_W}(?:\s{_W}){{1,3}}\b")
_SURNAME_END = re.compile(r"(?:ов|ев|ёв|ин|ын|ский|цкий|ская|цкая|ова|ева|ина|ына|ко|ук|юк|ян|дзе|швили|ых|их)"
                          r"(?:а|у|ым|ом|е|ой|ому|ого|им)?$")
_LABELS = ("EMAIL", "ОРГ", "РЕКВ", "СЧЁТ", "КАРТА", "СНИЛС", "ПАСПОРТ", "ДАТА", "АДРЕС", "ТАБ", "ТЕЛ", "ФИО")
_LABEL_RX = "|".join(_LABELS)
# Метки в ответе модели. Модель иногда слегка их искажает («[ФИО 1]», «ФИО_1») — такие тоже
# возвращаем, иначе сотрудник получил бы сообщение с техническими метками.
_TOKEN = re.compile(rf"\[\s*({_LABEL_RX})[_ ](\d+)\s*\]|(?<![\w\[])({_LABEL_RX})_(\d+)(?![\w\]])")


def _is_person(words: list) -> bool:
    """Серия слов с заглавной — человек? Точное словарное слово, либо два слова из словаря
    после снятия падежа, либо отчество + ещё одно словарное слово. Одно «похожее» слово в
    косвенном падеже — недостаточно: иначе «Главному Инженеру» сошло бы за ФИО."""
    hits = [_name_hit(w) for w in words]
    if 2 in hits:
        return True
    soft = sum(1 for h in hits if h)
    patronymic = any(_PATRONYMIC.search(_norm(w)) for w in words)
    return soft >= 2 or (soft >= 1 and patronymic and len(words) >= 2)


def _find_spans(text: str) -> list:
    """[(start, end, метка)] без пересечений: сначала форматы, потом ФИО по словарю."""
    spans = []

    def free(a, b):
        return all(b <= s or a >= e for s, e, _ in spans)

    for label, rx, grp in _PATTERNS:
        for m in rx.finditer(text):
            a, b = m.span(grp)
            if a < b and free(a, b):
                spans.append((a, b, label))
    for m in _FIO_RUN.finditer(text):
        words, a = m.group(0).split(), m.start()
        # «Звонить Сидоров Пётр Петрович»: слово начала фразы не имя — отрезаем его,
        # если оно не в словаре и не похоже на фамилию по окончанию.
        while len(words) > 2 and not _name_hit(words[0]) and not _SURNAME_END.search(_norm(words[0])):
            a += len(words[0]) + 1
            words = words[1:]
        if _is_person(words) and free(a, m.end()):
            spans.append((a, m.end(), "ФИО"))
    return sorted(spans)


class Masker:
    """Одна замена на один вызов модели: одинаковое значение -> одна и та же метка."""

    def __init__(self):
        self.by_token: dict = {}
        self._by_value: dict = {}
        self._n: dict = {}

    def mask(self, text: str) -> str:
        if not text or not enabled():
            return text
        out, pos = [], 0
        for a, b, label in _find_spans(text):
            value = text[a:b]
            token = self._by_value.get((label, value))
            if token is None:
                self._n[label] = self._n.get(label, 0) + 1
                token = f"[{label}_{self._n[label]}]"
                self._by_value[(label, value)] = token
                self.by_token[token] = value
            out.append(text[pos:a])
            out.append(token)
            pos = b
        out.append(text[pos:])
        return "".join(out)

    def unmask(self, text: str, json_safe: bool = False) -> str:
        """Вернуть реальные значения на место меток. json_safe — ответ модели в JSON: значение
        вставляется JSON-экранированным, иначе «ООО "Ромашка"» ломало разбор ответа."""
        if not text or not self.by_token:
            return text

        def repl(m):
            label, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            value = self.by_token.get(f"[{label}_{num}]")
            if value is None:
                return m.group(0)
            return json.dumps(value, ensure_ascii=False)[1:-1] if json_safe else value

        return _TOKEN.sub(repl, text)


def mask(text: str) -> str:
    """Разовое маскирование без обратной подстановки (для логов/хранения)."""
    return Masker().mask(text)


def looks_like_json(text: str) -> bool:
    head = (text or "").lstrip()[:8]
    return head.startswith(("{", "[", "```"))
