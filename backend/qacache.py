"""
qacache.py — кэш готовых ответов ассистента на частые вопросы.

Ключ = SHA-256 от нормализованного вопроса + должности + версии базы документов.
Нормализация: регистр, ё/е, пунктуация, короткие служебные слова, порядок слов —
поэтому «Где получить пропуск?» и «пропуск где получить» попадают в один ответ без
единого запроса к DeepSeek (маршрутизация + генерация стоят 2–3 запроса на вопрос).

Инвалидация: любое изменение документов (indexing.docs_changed) увеличивает версию базы —
старые ответы больше не находятся и доживают своё по TTL.

Хранение: Redis (общий для всех воркеров) или словарь в памяти процесса, если Redis нет.
ponytail: «похожесть» = совпадение набора значимых слов, без эмбеддингов. Перефразы
синонимами не поймает — для этого семантический кэш на pgvector (нужен провайдер эмбеддингов).
"""
import hashlib
import json
import re
import threading
from collections import OrderedDict

from redis_conn import get_redis

TTL = 7 * 24 * 3600
# Версия формата ответа: меняли промпт ответа (rag.GENERATE_SYSTEM) — увеличьте, и старые
# ответы из кэша больше не выдаются. 3 — без ссылок на документы и устройство системы.
ANSWER_FORMAT = 3
_MEM_MAX = 2000
_mem: "OrderedDict[str, str]" = OrderedDict()
_mem_ver = 0
_lock = threading.Lock()

# Служебные слова, не меняющие смысла вопроса. Отрицания («не», «ни», «нельзя») сюда НЕ
# входят: «Мне выдали пропуск?» и «Мне не выдали пропуск» — разные вопросы с разными ответами.
_STOP = {"а", "в", "во", "и", "к", "ко", "на", "о", "об", "от", "по", "с", "со", "у", "за", "из",
         "для", "до", "ли", "же", "бы", "то", "это", "мне", "я", "мы", "мой", "моя",
         "мои", "нам", "нас", "меня", "как", "подскажите", "скажите", "пожалуйста", "можно"}


def normalize(question: str) -> str:
    text = (question or "").lower().replace("ё", "е")
    words = [w for w in re.findall(r"[a-zа-я0-9]+", text) if w not in _STOP]
    return " ".join(sorted(set(words)))


def _version() -> int:
    r = get_redis()
    if r is None:
        return _mem_ver
    return int(r.get("nm:qa:ver") or 0)


def bump_version():
    """Документы изменились — все закэшированные ответы устарели."""
    global _mem_ver
    r = get_redis()
    if r is None:
        with _lock:
            _mem_ver += 1
            _mem.clear()
        return
    r.incr("nm:qa:ver")


def _key(question: str, position: str = "") -> str:
    norm = normalize(question)
    if not norm:
        return ""
    h = hashlib.sha256(f"{norm}|{(position or '').strip().lower()}".encode("utf-8")).hexdigest()
    return f"nm:qa:{ANSWER_FORMAT}.{_version()}:{h}"


def get(question: str, position: str = ""):
    key = _key(question, position)
    if not key:
        return None
    r = get_redis()
    if r is None:
        with _lock:
            raw = _mem.get(key)
            if raw is not None:
                _mem.move_to_end(key)
    else:
        raw = r.get(key)
    return json.loads(raw) if raw else None


def put(question: str, position: str, payload: dict):
    key = _key(question, position)
    if not key:
        return
    raw = json.dumps(payload, ensure_ascii=False)
    r = get_redis()
    if r is None:
        with _lock:
            _mem[key] = raw
            _mem.move_to_end(key)
            while len(_mem) > _MEM_MAX:
                _mem.popitem(last=False)
        return
    r.set(key, raw, ex=TTL)
