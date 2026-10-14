"""
Разбор документа БЕЗ ИИ: деление на смысловые фрагменты, темы фрагмента и кандидаты-подэтапы —
классическими алгоритмами информационного поиска. Нейросеть (локальная модель) потом только
выбирает из кандидатов, которые нашли эти алгоритмы (pipeline._hybrid_label). Точность ниже,
чем у «модель решает всё», зато основа разбора не зависит от ИИ и воспроизводима.
Чистая логика: без сети, БД и сторонних библиотек — тестируется целиком.

Сегментация — три независимых алгоритма голосуют за границы между предложениями:
  texttiling — TextTiling (Hearst, 1997): лексическая связность соседних окон предложений;
               граница там, где связность проседает глубже среднего («долина»);
  c99        — C99 (Choi, 2000): матрица сходства предложений -> ранговая матрица ->
               деление сверху вниз, пока растёт плотность сходства внутри фрагментов;
  cues       — разметка самого текста: нумерация пунктов («5.», «5.1»), «Статья/Раздел/Глава»,
               заголовок прописными.
Граница принимается при ≥ 2 голосах; слишком длинный фрагмент дорезается по лучшей из
оставшихся границ, слишком короткий приклеивается к соседу. Голоса всех алгоритмов
сохраняются (полная выгрузка — scripts/export_segmentation.py).

Темы фрагмента — без описаний плана: ключевые слова TF-IDF (основа слова + частая словоформа).
Сопоставление с планом — описание подэтапа лишь уточняющий материал: TF-IDF-косинус фрагмента
с «профилем» подэтапа (название, бриф, «ВКЛЮЧАТЬ», теги) минус сходство с «НЕ ВКЛЮЧАТЬ».
"""

import math
import re
from collections import Counter

# ---------- Слова: токены, стоп-слова, основа слова (Snowball для русского) ----------
_STOP = set("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже для до его
ее ей если есть еще же за здесь и из или им их к как ко когда кто ли либо между меня мне может мы на
над надо наш не него нее нет ни них но ну о об однако он она они оно от очень по под после при про с
со так также такой там те тем то того тоже той только том ты у уже хотя чего чей чем что чтобы чье
эта эти это этого этой этом этот я который которая которые которых котором которой которую каждый
также т д п др см т.е тч свой своя свои своего своей своих сам сама само сами является являются
""".split())
_VOWELS = "аеиоуыэюя"
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\d-]*")
_ABBR = re.compile(r"^[A-ZА-ЯЁ]{2,6}$")

_PERF = re.compile(r"(?:(?<=[ая])(?:вшись|вши|в)|ившись|ывшись|ивши|ывши|ив|ыв)$")
_REFL = re.compile(r"(?:ся|сь)$")
_ADJ = re.compile(r"(?:ими|ыми|его|ого|ему|ому|ее|ие|ые|ое|ей|ий|ый|ой|ем|им|ым|ом|их|ых|ую|юю|ая|яя|ою|ею)$")
_PART = re.compile(r"(?:(?<=[ая])(?:ем|нн|вш|ющ|щ)|ивш|ывш|ующ)$")
_VERB = re.compile(r"(?:(?<=[ая])(?:ете|йте|ешь|нно|ла|на|ли|ем|ло|но|ет|ют|ны|ть|й|л|н)|ейте|уйте|ила|ыла|ена|"
                   r"ите|или|ыли|ило|ыло|ено|ует|уют|ены|ить|ыть|ишь|ей|уй|ил|ыл|им|ым|ен|ят|ит|ыт|ую|ю)$")
_NOUN = re.compile(r"(?:иями|ями|ами|ией|иям|ием|иях|ев|ов|ие|ье|еи|ии|ей|ой|ий|ям|ем|ам|ом|ах|ях|ию|ью|ия|ья|"
                   r"а|е|и|й|о|у|ы|ь|ю|я)$")


def _r(w: str, start: int) -> int:
    """Начало области R по Snowball: после первой согласной, следующей за гласной."""
    for i in range(start + 1, len(w)):
        if w[i] not in _VOWELS and w[i - 1] in _VOWELS:
            return i + 1
    return len(w)


def stem(word: str) -> str:
    """Основа слова по алгоритму Snowball (Портер) для русского: «охрана/охраны/охране» -> «охран»."""
    w = word.lower().replace("ё", "е")
    m = re.search(f"[{_VOWELS}]", w)
    if not m:
        return w
    pre, rv = w[:m.end()], w[m.end():]
    out, n = _PERF.subn("", rv)
    if not n:
        rv = _REFL.sub("", rv)
        out, n = _ADJ.subn("", rv)
        if n:
            out = _PART.sub("", out)
        else:
            out, n = _VERB.subn("", rv)
            if not n:
                out = _NOUN.sub("", rv)
    rv = re.sub("и$", "", out)
    r2 = _r(w, _r(w, 0) - 1) if _r(w, 0) < len(w) else len(w)
    d = re.search("ость?$", rv)
    if d and len(pre) + d.start() >= r2:
        rv = rv[:d.start()]
    if rv.endswith("нн"):
        rv = rv[:-1]
    else:
        out, n = re.subn("ейше?$", "", rv)
        rv = (out[:-1] if out.endswith("нн") else out) if n else re.sub("ь$", "", rv)
    return pre + rv


def tokens(text: str) -> list:
    """[(основа, словоформа)] значимых слов. Аббревиатуры прописными (СИЗ, ОТ, ЧПУ) — как есть."""
    out = []
    for m in _WORD.finditer(text or ""):
        w = m.group(0).strip("-")
        if _ABBR.match(w):
            out.append((w.upper().replace("Ё", "Е"), w))
            continue
        low = w.lower().replace("ё", "е")
        if len(low) >= 3 and low not in _STOP and not low[0].isdigit():
            out.append((stem(low), low))
    return out


def bag(text: str) -> Counter:
    return Counter(s for s, _ in tokens(text))


# ---------- Векторы и сходство ----------
def idf_table(bags: list) -> dict:
    """IDF по набору «документов» (мешков основ): редкое слово весомее частого."""
    n, df = len(bags), Counter()
    for b in bags:
        df.update(b.keys())
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def weigh(b: Counter, idf: dict) -> dict:
    return {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in b.items()}


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(t, 0.0) for t, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


# ---------- Единицы текста: предложения + признак разметки ----------
_SENT = re.compile(r"(?<=[.!?…])\s+(?=[«\"“(\[A-ZА-ЯЁ0-9])")
_CUE_NUM = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){0,3}[.)]?\s+[A-ZА-ЯЁ«\"]")
_CUE_WORD = re.compile(r"^\s*(?:статья|раздел|глава|часть|приложение)\s+[\dIVXА-ЯЁ]", re.I)
_CUE_CAPS = re.compile(r"^[^a-zа-яё]*[A-ZА-ЯЁ]{3,}(?:[\s,-]+[A-ZА-ЯЁ]{2,}){1,8}[^a-zа-яё]*$")


def units(text: str) -> list:
    """Предложения документа: [{"text", "cue"}]; cue — строка начинается с пункта/заголовка."""
    out = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        cue = bool(_CUE_NUM.match(line) or _CUE_WORD.match(line) or _CUE_CAPS.match(line))
        for i, s in enumerate(p for p in _SENT.split(line) if p.strip()):
            out.append({"text": s.strip(), "cue": cue and i == 0})
    return out


# ---------- Сегментация: три алгоритма ----------
def texttiling(vecs: list, k: int = 0) -> dict:
    """TextTiling: {граница (индекс предложения, с которого начинается новый фрагмент): глубина}."""
    n = len(vecs)
    if n < 4:
        return {}
    k = k or max(2, min(5, n // 6))
    gaps = []
    for g in range(1, n):
        left, right = Counter(), Counter()
        for v in vecs[max(0, g - k):g]:
            left.update(v)
        for v in vecs[g:g + k]:
            right.update(v)
        gaps.append(cosine(left, right))
    s = [sum(gaps[max(0, i - 1):i + 2]) / len(gaps[max(0, i - 1):i + 2]) for i in range(len(gaps))]
    depth = {}
    for i, v in enumerate(s):
        if (i and s[i - 1] < v) or (i + 1 < len(s) and s[i + 1] < v):
            continue                                    # не дно «долины»
        lp = i
        while lp > 0 and s[lp - 1] >= s[lp]:
            lp -= 1
        rp = i
        while rp + 1 < len(s) and s[rp + 1] >= s[rp]:
            rp += 1
        depth[i + 1] = (s[lp] - v) + (s[rp] - v)
    if not depth:
        return {}
    vals = list(depth.values())
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals))
    return {g: d for g, d in depth.items() if d > 0 and d > mean - sd / 2}


def c99(vecs: list, mask: int = 11, c: float = 1.2) -> set:
    """C99: ранговая матрица сходства и деление сверху вниз. -> множество границ."""
    n = len(vecs)
    if n < 4:
        return set()
    sim = [[cosine(vecs[i], vecs[j]) for j in range(n)] for i in range(n)]
    r = mask // 2
    rank = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            lo_i, hi_i, lo_j, hi_j = max(0, i - r), min(n, i + r + 1), max(0, j - r), min(n, j + r + 1)
            cells = (hi_i - lo_i) * (hi_j - lo_j) - 1
            if cells > 0:
                less = sum(1 for x in range(lo_i, hi_i) for y in range(lo_j, hi_j) if sim[x][y] < sim[i][j])
                rank[i][j] = less / cells
    pre = [[0.0] * (n + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(n):
            pre[i + 1][j + 1] = rank[i][j] + pre[i][j + 1] + pre[i + 1][j] - pre[i][j]

    def block(a, b):
        return pre[b][b] - pre[a][b] - pre[b][a] + pre[a][a]

    def density(bounds):
        b = sorted(bounds)
        return (sum(block(b[i], b[i + 1]) for i in range(len(b) - 1))
                / sum((b[i + 1] - b[i]) ** 2 for i in range(len(b) - 1)))

    bounds, order, dens = [0, n], [], [density([0, n])]
    for _ in range(min(n - 1, max(1, n // 2))):
        best = max((g for g in range(1, n) if g not in bounds), key=lambda g: density(bounds + [g]), default=None)
        if best is None:
            break
        bounds.append(best)
        order.append(best)
        dens.append(density(bounds))
    grad = [dens[i] - dens[i - 1] for i in range(1, len(dens))]
    if len(grad) < 2:
        return set(order[:1]) if grad and grad[0] > 0 else set()
    mu = sum(grad) / len(grad)
    sd = math.sqrt(sum((g - mu) ** 2 for g in grad) / len(grad))
    keep = 0
    for g in grad:
        if g <= mu + c * sd and keep:
            break
        keep += 1
    return set(order[:keep])


MIN_CHARS = 250
MAX_CHARS = 1500


def segment(text: str, max_chars: int = MAX_CHARS, min_chars: int = MIN_CHARS) -> list:
    """Делит текст на смысловые фрагменты. -> [{"text", "votes": {алгоритм: bool}, "score"}]:
    votes — кто из алгоритмов голосовал за границу ПЕРЕД фрагментом (у первого — пусто)."""
    us = units(text)
    if not us:
        return []
    vecs = [bag(u["text"]) for u in us]
    depth = texttiling(vecs)
    found = {"texttiling": set(depth), "c99": c99(vecs),
             "cues": {i for i, u in enumerate(us) if u["cue"] and i}}
    # Алгоритмы часто расходятся на одно предложение (C99 — 11, TextTiling — 12): соседние
    # кандидаты (±1) — одна граница; ставим её туда, где больше точных голосов.
    votes, run = {}, []
    for g in sorted(set().union(*found.values())) + [None]:
        if run and (g is None or g - run[0] > 2):
            pos = max(run, key=lambda x: (sum(x in s for s in found.values()), depth.get(x, 0.0)))
            votes[pos] = {name: any(x in s for x in run) for name, s in found.items()}
            run = []
        if g is not None:
            run.append(g)
    weight = {g: sum(v.values()) + depth.get(g, 0.0) for g, v in votes.items()}
    cut = sorted(g for g, v in votes.items() if sum(v.values()) >= 2)

    def size(a, b):
        return sum(len(u["text"]) + 1 for u in us[a:b])

    # Длинный фрагмент — дорезаем по самой «голосистой» из оставшихся границ внутри.
    changed = True
    while changed:
        changed = False
        bounds = [0] + cut + [len(us)]
        for a, b in zip(bounds, bounds[1:]):
            if size(a, b) > max_chars and b - a > 1:
                g = max(range(a + 1, b), key=lambda x: (weight.get(x, 0.0), -abs(size(a, x) - size(x, b))))
                cut = sorted(cut + [g])
                changed = True
                break
    # Короткий фрагмент — убираем границу со стороны более слабого голоса.
    changed = True
    while changed and cut:
        changed = False
        bounds = [0] + cut + [len(us)]
        for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
            if size(a, b) < min_chars:
                sides = [g for g in (a, b) if g in cut]
                if sides:
                    cut.remove(min(sides, key=lambda g: weight.get(g, 0.0)))
                    changed = True
                    break
    bounds = [0] + cut + [len(us)]
    return [{"text": " ".join(u["text"] for u in us[a:b]),
             "votes": votes.get(a, {}) if a else {},
             "score": round(weight.get(a, 0.0), 3) if a else None}
            for a, b in zip(bounds, bounds[1:])]


# ---------- Темы фрагмента: ключевые слова TF-IDF ----------
def keywords(text: str, idf: dict, top: int = 8) -> list:
    """[(слово, вес)] — самые характерные для фрагмента слова (частая словоформа основы)."""
    toks = tokens(text)
    tf, forms = Counter(), {}
    for s, w in toks:
        tf[s] += 1
        forms.setdefault(s, Counter())[w] += 1
    scored = sorted(weigh(tf, idf).items(), key=lambda kv: -kv[1])[:top]
    return [(forms[s].most_common(1)[0][0], round(v, 2)) for s, v in scored]


# ---------- Кандидаты-подэтапы ----------
SIM_FULL = 0.25    # сходство, при котором уверенность = 1
MIN_SIM = 0.06     # ниже — подэтап даже не кандидат


def plan_profiles(structure: dict, extra: dict = None) -> list:
    """Профили подэтапов: [{"id", "title", "text", "exclude"}]. extra — {id: {"tags", "include",
    "exclude"}} из каталога (focus): описание плана — уточняющий материал к тексту фрагмента."""
    out = []
    for st in (structure or {}).get("stages") or []:
        for sub in st.get("substages") or []:
            x = (extra or {}).get(sub.get("id")) or {}
            title = f"{st.get('title', '')} {sub.get('title', '')}"
            out.append({"id": sub["id"], "title": f"{st.get('title', '')} / {sub.get('title', '')}",
                        "brief": (sub.get("description") or "")[:200],
                        "text": " ".join([title, sub.get("title", ""), sub.get("description") or "",
                                          x.get("include") or "", " ".join(x.get("tags") or [])]),
                        "exclude": x.get("exclude") or ""})
    return out


class Matcher:
    """TF-IDF-сходство фрагментов документа с профилями подэтапов. IDF — по фрагментам этого
    документа и профилям вместе: общие для всего документа слова («работник», «предприятие»)
    весят мало, специфичные для темы — много."""

    def __init__(self, profiles: list, texts: list):
        self.profiles = profiles
        p_bags = [bag(p["text"]) for p in profiles]
        self.idf = idf_table(p_bags + [bag(t) for t in texts])
        self.vec = [weigh(b, self.idf) for b in p_bags]
        self.excl = [weigh(bag(p["exclude"]), self.idf) if p["exclude"] else {} for p in profiles]

    def candidates(self, text: str, k: int = 5) -> list:
        """[{"id", "title", "score", "confidence"}] по убыванию сходства, только >= MIN_SIM."""
        v = weigh(bag(text), self.idf)
        scored = []
        for p, pv, ex in zip(self.profiles, self.vec, self.excl):
            s = cosine(v, pv) - (0.5 * cosine(v, ex) if ex else 0.0)
            if s >= MIN_SIM:
                scored.append({"id": p["id"], "title": p["title"], "score": round(s, 4),
                               "confidence": round(min(1.0, s / SIM_FULL), 2)})
        scored.sort(key=lambda x: -x["score"])
        return scored[:k]


# ---------- Служебный текст и должности ----------
_GENERAL = re.compile(
    r"(?i)настоящ\w+\s+(?:положени|инструкци|регламент|стандарт|порядок|правил|программ)\w*\s+"
    r"(?:разработан|определя|устанавлива|регламентиру|распространя)|област\w+\s+применения|"
    r"термины\s+и\s+определения|нормативные\s+ссылки|сфер\w+\s+действия|вступа\w+\s+в\s+силу|"
    r"утвержд\w+\s+приказом|лист\s+ознакомления|лист\s+согласования|лист\s+регистрации\s+изменений")


def is_boilerplate(text: str) -> bool:
    """Преамбула, область применения, термины, листы ознакомления — служебная «обвязка» документа."""
    return bool(_GENERAL.search(text or ""))


def professions_in(text: str, positions: list) -> list:
    """Должности штатного расписания, которые прямо названы во фрагменте (все значимые слова
    названия должности есть во фрагменте, в любой форме)."""
    have = set(bag(text))
    out = []
    for pos in positions or []:
        need = set(bag(pos))
        if need and need <= have and pos not in out:
            out.append(pos)
    return out


_DOC_TYPES = ("положение", "инструкция", "регламент", "приказ", "правила", "программа", "порядок",
              "стандарт", "договор", "памятка", "перечень", "методика", "политика", "кодекс")


def doc_card(title: str, toc: str, head_text: str, positions: list = None) -> dict:
    """Карточка документа без модели: тип по названию, кратко — первый заголовок, адресаты —
    названные в начале должности."""
    head = f"{title}\n{toc}\n{head_text}"
    low = head.lower()
    doc_type = next((t for t in _DOC_TYPES if t in low[:600]), "документ")
    first = next((ln.strip("# ").strip() for ln in (toc or head_text or "").splitlines()
                  if len(ln.strip("# ").strip()) > 5), title)
    audience = professions_in(head_text, positions or [])
    scope = "general" if not audience else ("mono_profession" if len(audience) == 1 else "multi_profession")
    return {"doc_type": doc_type, "summary": first[:200], "audience": audience or ["все"], "scope": scope,
            "source": "lexical"}
