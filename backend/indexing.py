"""
Приём документов и реестр. Используется веб-приложением (app.py) и worker-процессами.

Путь документа от загрузки до готовности:

    оригинал (pdf/docx/...), хранится ОДИН раз плоско в data/documents/ (+ S3)
        -> docpipe.ingest(): docling разбирает документ на секции, DeepSeek размечает
           каждую секцию относительно плана адаптации (этапы/подэтапы/профессии) и
           пишет метки в Postgres. Векторов и эмбеддингов нет — ретрив идёт по
           LLM-меткам (см. rag.route_substages -> docpipe.blocks_for_substage).

Здесь же: сохранение загруженного файла, фоновая очередь индексации, реестр
документов и повторный анализ. Сам разбор/разметка живут в пакете docpipe.
"""

from __future__ import annotations  # аннотации-строки: тип DoclingDocument не грузит docling при импорте

import re
import time
import uuid
import queue
import hashlib
import threading
from pathlib import Path
from typing import Optional

# docling НЕ импортируем на уровне модуля: он тяжёлый (модели разметки, ~ГБ RAM), а
# нужен только при разборе документа, что теперь делают worker-процессы, а не web.
# Ленивая загрузка ниже (_converter) — web-воркеры не платят за docling памятью/стартом.

import documents
import storage
import docregistry
from config import (
    DOCS_DIR, CONVERTED_DIR, CACHE_DIR,
    SUPPORTED_EXT, MAX_UPLOAD_BYTES,
)

# Символическое перекрытие между соседними чанками (ТЗ §12): в текст для эмбеддинга
# следующего чанка добавляется «хвост» предыдущего, чтобы информация на границе не
# терялась. Доля от чанка, а не жёсткое число — грубая, но рабочая эвристика.
# ponytail: фиксированная доля; при желании стратегию можно усложнить под модель/структуру.
OVERLAP_CHARS = 240

MAX_TOKENS = 512     # бюджет токенов на чанк (окно чанкера docling)
MERGE_PEERS = True   # склеивать соседние мелкие чанки одного уровня иерархии

_converter = None
_chunker = None
_docling_lock = threading.Lock()


def _get_converter():
    """Ленивый singleton docling — грузится при первом разборе (в worker-процессе)."""
    global _converter, _chunker
    if _converter is None:
        with _docling_lock:
            if _converter is None:
                from docling.document_converter import DocumentConverter
                from docling.chunking import HybridChunker
                _chunker = HybridChunker(max_tokens=MAX_TOKENS, merge_peers=MERGE_PEERS)
                _converter = DocumentConverter()
    return _converter

def _log(step, msg):
    print(f"[INDEX:{step}] {msg}")


# ---------- Реестр документов ----------
# Само хранилище — docregistry.py (таблица doc_registry в PostgreSQL). Здесь — только
# реэкспорт публичных имён (indexing.list_documents и т.п. зовут снаружи).
_update_registry = docregistry.update
list_documents = docregistry.list_documents
set_clarification = docregistry.set_clarification
folder_doc_counts = docregistry.folder_doc_counts


# ---------- Вспомогательные функции ----------
def safe_filename(filename: str) -> str:
    """Убираем путь и опасные символы — защита от path traversal при загрузке."""
    name = Path(filename or "").name
    # Скобки допустимы: «Регламент (2).pdf» — так называется отдельно сохранённая версия.
    name = re.sub(r"[^\w\-.() а-яА-ЯёЁ]", "_", name).strip()
    if not name.strip(".") or name.startswith("."):
        name = ""
    if len(name) > 200:                   # длинное имя режем, сохраняя расширение
        ext = Path(name).suffix[:10]
        name = name[:200 - len(ext)] + ext
    return name or f"document_{uuid.uuid4().hex[:8]}"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]




def extract_page_no(chunk) -> Optional[int]:
    try:
        return chunk.meta.doc_items[0].prov[0].page_no
    except (AttributeError, IndexError, TypeError):
        return None


# ---------- Конвертация с кэшированием ----------
def _detect_rotated_pages(path: Path) -> dict:
    """{индекс_страницы: угол_текста°} для страниц, где текст нарисован боком
    (ландшафтные таблицы без флага /Rotate). Угол — из pdfium (FPDFText_GetCharAngle,
    против часовой). Ровные страницы (0°) в результат не попадают."""
    import math
    import statistics
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    out = {}
    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            tp = pdf[i].get_textpage()
            try:
                n = tp.count_chars()
                if n < 20:          # мало текста — угол ненадёжен, пропускаем
                    continue
                step = max(1, n // 200)   # выборка символов, не весь текст
                angs = [pdfium_c.FPDFText_GetCharAngle(tp.raw, k) for k in range(0, n, step)]
                angs = [a for a in angs if a is not None and a >= 0]
                if not angs:
                    continue
                deg = round(math.degrees(statistics.median(angs))) % 360
                if deg in (90, 270):
                    out[i] = deg
            finally:
                tp.close()
    finally:
        pdf.close()
    return out


def _normalize_rotation(filepath: Path):
    """Разворачивает страницы с боком-нарисованным текстом, чтобы docling читал их
    правильно (иначе таблица приходит перемешанной). Возвращает (путь_для_docling,
    временный_ли_файл). Не-PDF и любой сбой -> исходный файл (индексацию не роняем)."""
    if filepath.suffix.lower() != ".pdf":
        return filepath, False
    try:
        rotated = _detect_rotated_pages(filepath)
        if not rotated:
            return filepath, False
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(str(filepath))
        writer = PdfWriter()
        for i, page in enumerate(reader.pages):
            if i in rotated:
                page.rotate((360 - rotated[i]) % 360)   # компенсируем угол текста
            writer.add_page(page)
        tmp = CACHE_DIR / f"{filepath.stem}__norot_{file_hash(filepath)}.pdf"
        with open(tmp, "wb") as f:
            writer.write(f)
        print(f"[index] {filepath.name}: развёрнуто повёрнутых страниц: {len(rotated)}")
        return tmp, True
    except Exception as e:
        print(f"[index] нормализация поворота не удалась ({filepath.name}): {e}")
        return filepath, False


def convert_document(filepath: Path) -> DoclingDocument:
    """
    Гоняем файл через docling. Разбор PDF/DOCX (особенно PDF с layout-моделью)
    — самая тяжёлая часть пайплайна, поэтому результат кэшируется в JSON:
    при повторной обработке того же файла (переезд между папками, смена
    параметров чанкинга) можно переиспользовать готовую структуру документа.
    """
    # если локальной копии нет — тянем оригинал из S3 по ключу из реестра
    # (структура <суперадмин>/<админ>/<файл>; у старых записей ключа нет — плоский)
    from docling_core.types.doc.document import DoclingDocument
    storage.pull(filepath, (docregistry.get(filepath.name) or {}).get("s3_key") or "")
    cache_path = CACHE_DIR / f"{filepath.stem}__{file_hash(filepath)}.json"
    if cache_path.exists():
        return DoclingDocument.load_from_json(str(cache_path))

    # Повёрнутые страницы (боком-нарисованные таблицы) выправляем до docling.
    src, is_tmp = _normalize_rotation(filepath)
    try:
        result = _get_converter().convert(str(src))
    finally:
        if is_tmp:
            try:
                src.unlink()
            except OSError:
                pass
    doc = result.document
    doc.save_as_json(str(cache_path))
    return doc


# Номер пункта регламента в начале строки: «5», «5.1», «5.1.2», с точкой или скобкой.
# Регламенты часто нумеруют пункты цифрами без стилей заголовков — docling такой
# документ отдаёт сплошным текстом, и без этого деления один пункт не отделить от другого.
CLAUSE_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+){0,3})[.)]?[ \t]+(?=\S)")


def sectionize_by_clause(text: str) -> list:
    """
    Делит текст чанка на пункты по номерам в начале строки («5.1 ...»).
    Возвращает [(section_label, segment_text), ...]. Меньше двух номеров —
    возвращаем чанк целиком (section = единственный номер или None).
    Применяется только когда у чанка нет заголовков от docling (см. index_document).
    """
    marks = [(m.start(), m.group(1)) for m in CLAUSE_RE.finditer(text)]
    if len(marks) < 2:
        label = marks[0][1] if marks else None
        return [(label, text.strip())]
    segments = []
    for i, (pos, num) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        seg = text[pos:end].strip()
        if seg:
            segments.append((num, seg))
    return segments


# ---------- Индексация одного документа ----------
def _docpipe_progress_cb(filename: str):
    """Колбэк прогресса docpipe-разметки -> прогресс-бар на карточке документа.
    Троттлинг раз в секунду; секции маппим в 10..95%, чтобы бар двигался и не выглядел
    зависшим на больших документах (десятки секций × вызов DeepSeek на секцию)."""
    last = [0.0]
    def cb(done, total):
        now = time.time()
        if done < total and now - last[0] < 1.0:
            return
        last[0] = now
        pct = min(95, 10 + int(85 * done / max(total, 1)))
        _update_registry(filename, progress=pct, phase=f"Классификация docpipe: {done}/{total}")
    return cb


def docs_changed():
    """База документов изменилась -> обновить сообщения планов, у которых они уже есть.
    Модель трогает только подэтапы, чьи документы поменялись (отпечатки в planner) —
    админу не нужно помнить про «Догенерировать недостающее»."""
    try:
        import qacache
        qacache.bump_version()             # закэшированные ответы на вопросы устарели
    except Exception as e:
        _log("QA", f"кэш ответов не сброшен: {e}")
    try:
        import planner
        n = planner.refresh_generated_plans()
        if n:
            _log("GEN", f"автообновление сообщений: запущено для планов — {n}")
    except Exception as e:
        _log("GEN", f"автообновление сообщений не запущено: {e}")


def index_document(filepath: Path) -> dict:
    """Приём документа: docling-разбор + классификация/разметка через docpipe
    (этапы/подэтапы, метки в Postgres). Векторов/эмбеддингов нет — ретрив идёт по
    LLM-меткам docpipe (см. rag.route_substages, planner через docpipe.chunks_for_substage)."""
    filename = filepath.name
    if is_confidential(filename):
        # Флаг поставили, пока файл ждал в очереди, — в модель его не отправляем.
        return {"filename": filename, "status": "confidential", "chunks": 0}
    _update_registry(filename, status="processing", error=None,
                     phase="Классификация (docpipe)", progress=10)
    try:
        import docpipe
        start = time.time()
        res = docpipe.ingest(str(filepath), filename=filename, force=True,
                             progress_cb=_docpipe_progress_cb(filename))
        sections = res.get("sections") or 0
        elapsed = round(time.time() - start, 2)
        _update_registry(filename, status="indexed", chunks=sections, error=None,
                         indexed_in_seconds=elapsed, phase=None, progress=100, path=filename)
        _log("DONE", f"{filename}: классифицирован docpipe, секций {sections} за {elapsed} с")
        docs_changed()
        return {"filename": filename, "status": "indexed", "chunks": sections, "elapsed": elapsed}
    except Exception as e:
        _update_registry(filename, status="error", error=str(e))
        return {"filename": filename, "status": "error", "error": str(e)}




def delete_document(filename: str, remove_file: bool = True) -> bool:
    """Полное удаление документа: разметка docpipe (Postgres) + оригинал + markdown +
    кэш + запись в реестре. Векторов больше нет."""
    try:
        from docpipe import store as _dp_store
        # Удаляем ВСЕ строки docpipe с этим именем (не только первую): иначе дубль или
        # рассинхрон имени оставит осиротевшие метки, и удалённый документ продолжит
        # цитироваться в генерации/Q&A как «призрачный источник».
        n = _dp_store.delete_by_filename(filename)
        _log("DELETE", f"docpipe: удалено документов {n} для {filename}")
    except Exception as e:
        _log("DELETE", f"docpipe-разметка {filename}: {e}")

    entry = docregistry.get(filename)

    if entry and remove_file:
        rel_path = entry.get("path", filename)
        filepath = DOCS_DIR / rel_path
        # убираем оригинал из S3 по ключу владельца (no-op, если S3 выключен)
        storage.delete(rel_path, entry.get("s3_key") or "")
        if filepath.exists():
            filepath.unlink()
            for cache_file in CACHE_DIR.glob(f"{filepath.stem}__*.json"):
                cache_file.unlink()
        md_rel = entry.get("markdown_path")
        if md_rel:
            md_path = CONVERTED_DIR / md_rel
            if md_path.exists():
                md_path.unlink()

    existed = docregistry.remove(filename)

    # Синхронно убираем строку из реестра метаданных, чтобы экран не показывал удалённое.
    try:
        documents.remove_by_filename(filename)
    except Exception as e:
        _log("DELETE", f"метаданные {filename} из БД: {e}")
    return existed


# ---------- Приём загруженного файла (используется веб-ручкой upload) ----------
def owner_dirs(uploader: Optional[dict] = None) -> tuple:
    """
    Пара папок хранилища для загрузки: (папка суперадмина, папка загрузившего).
    Структура в S3 — <суперадмин>/<администратор>/<файл>: всё, что грузят
    администраторы, лежит внутри папки суперадмина. Если загрузивший неизвестен
    (CLI-индексация), кладём в папку суперадмина как в общую.
    """
    import users
    top = users.dir_slug(users.get_owner())
    return top, (users.dir_slug(uploader) if uploader else top)


def free_filename(filename: str) -> str:
    """«Регламент.pdf» занят -> «Регламент (2).pdf» (первое свободное имя)."""
    taken = {d.get("filename") for d in list_documents()}
    stem, ext = Path(filename).stem, Path(filename).suffix
    n = 2
    while f"{stem} ({n}){ext}" in taken or (DOCS_DIR / f"{stem} ({n}){ext}").exists():
        n += 1
    return f"{stem} ({n}){ext}"


def find_duplicate(content: bytes) -> Optional[dict]:
    """Уже загруженный документ с тем же содержимым (кем угодно) — его не обрабатываем и не
    оплачиваем повторно. Сначала SHA-256 из реестра; для документов, загруженных до того,
    как реестр стал хранить хэш, — по хэшу содержимого в таблице разметки docpipe."""
    digest = hashlib.sha256(content).hexdigest()
    found = docregistry.find_by_sha256(digest)
    if found:
        return found
    try:
        import db
        row = db.query("SELECT filename FROM documents WHERE content_hash = %s LIMIT 1", (digest[:32],), "one")
    except Exception:
        row = None
    return docregistry.get(row["filename"]) if row else None


def is_confidential(filename: str) -> bool:
    return bool((docregistry.get(filename) or {}).get("confidential"))


def set_confidential(filename: str, confidential: bool) -> dict:
    """«Не отправлять в ИИ» — для чувствительных документов (положение об оплате труда и т.п.).
    Включить: разметка документа удаляется (его фрагменты больше не попадают ни в генерацию
    сообщений, ни в ответы на вопросы), файл остаётся в базе. Выключить: документ уходит на
    обычную обработку. Возвращает запись реестра."""
    if confidential:
        try:
            from docpipe import store as _dp_store
            _dp_store.delete_by_filename(filename)
        except Exception as e:
            _log("CONFIDENTIAL", f"разметка {filename} не удалена: {e}")
        try:
            documents.remove_by_filename(filename)
        except Exception:
            pass
        entry = _update_registry(filename, confidential=True, status="confidential", chunks=0,
                                 phase=None, progress=None, error=None)
    else:
        entry = _update_registry(filename, confidential=False, status="uploaded", error=None)
        enqueue_document(DOCS_DIR / filename)
    return entry


def save_uploaded_file(filename: str, content: bytes, uploader: Optional[dict] = None,
                       confidential: bool = False) -> Path:
    filename = safe_filename(filename)
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"Неподдерживаемый формат файла: {ext or '(нет расширения)'}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024*1024)} МБ")

    # Локально файл лежит плоско в data/documents/ — это КЭШ для конвейера docling
    # (папки — логические метки, физических копий не создают, ТЗ §2). Структура
    # «суперадмин/администратор» живёт в durable-хранилище S3: см. storage.doc_key.
    filepath = DOCS_DIR / filename
    with open(filepath, "wb") as f:
        f.write(content)
    try:
        top, own = owner_dirs(uploader)
    except Exception as e:            # БД недоступна — не срываем загрузку
        _log("STORAGE", f"не удалось определить папку владельца: {e}")
        top = own = ""
    s3_key = storage.doc_key(filename, top, own)
    storage.put(filename, content, s3_key)   # дублируем оригинал в S3 (no-op, если S3 выключен)

    _update_registry(
        filename,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        # Конфиденциальный документ хранится, но в ИИ не уходит: не размечается вовсе.
        status="confidential" if confidential else "uploaded",
        confidential=bool(confidential),
        chunks=0,
        folders=[],
        stage_ids=[],
        summary=None,
        error=None,
        # Владелец документа: по нему работает разграничение видимости (админ видит
        # только свои загрузки, суперадмин — все) и строится путь в хранилище.
        uploaded_by=(uploader or {}).get("id"),
        uploaded_by_name=(uploader or {}).get("full_name") or (uploader or {}).get("username") or "",
        uploaded_by_role=(uploader or {}).get("role"),
        department=(uploader or {}).get("department") or "",
        storage_path=f"{top}/{own}/{filename}" if top and own else filename,
        s3_key=s3_key,
    )
    return filepath


# ---------- Фоновая очередь индексации (используется веб-ручкой upload) ----------
# docling-разбор тяжёлый (layout PDF — секунды-минуты), поэтому загрузка через сайт
# не ждёт обработку синхронно, а ставит файл в очередь. Один воркер обрабатывает файлы
# по очереди (FIFO): параллельный docling на нескольких файлах съел бы всю память.
# ponytail: один глобальный воркер; если понадобится пропускная способность —
# несколько воркеров + семафор под память.
# Состояние задач индексации — в общем jobstore (Redis при мультипроцессе, иначе
# in-memory), чтобы прогресс был виден и web-воркеру, и worker-процессу.
import jobstore

_JOB_NS = "index"


def _set_index_job(job_id: str, **fields):
    return jobstore.set_job(_JOB_NS, job_id, **fields)


def get_index_job(job_id: str) -> Optional[dict]:
    return jobstore.get_job(_JOB_NS, job_id)


def process_index_job(job_id: str):
    """Обрабатывает одну задачу индексации. Выполняется в worker-процессе (RQ) или
    в daemon-потоке-фолбэке. Тяжёлый docling+LLM не держит web-воркер."""
    try:
        job = get_index_job(job_id)
        if not job:
            return
        filepath = Path(job["filepath"])
        _set_index_job(job_id, status="processing", started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        result = index_document(filepath)
        _set_index_job(job_id, status=result["status"], result=result,
                       finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    except Exception as e:
        _set_index_job(job_id, status="error", result={"status": "error", "error": str(e)})


def enqueue_document(filepath: Path) -> dict:
    """Ставит уже сохранённый файл в очередь на индексацию. Возвращает запись задачи."""
    import jobs
    job_id = str(uuid.uuid4())
    filename = Path(filepath).name
    _set_index_job(job_id, filename=filename, filepath=str(filepath),
                   status="queued", queued_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    jobs.enqueue_index(job_id)
    return get_index_job(job_id)


def requeue_stranded() -> int:
    """Возвращает в очередь документы, зависшие в 'uploaded'/'processing'. Очередь — в
    памяти процесса, поэтому рестарт приложения (или потерянная задача) оставлял бы такие
    файлы навсегда «Загружен», не индексируя. Зовётся при старте. Возвращает число."""
    n = 0
    for entry in list_documents():
        if entry.get("status") in ("uploaded", "processing"):
            enqueue_document(DOCS_DIR / entry["filename"])
            n += 1
    if n:
        print(f"[index] возвращено в очередь зависших документов: {n}")
    return n


# ---------- Повторный анализ (ТЗ §8, §26) ----------
def reanalyze_document(filename: str) -> dict:
    """Переклассифицировать документ заново через docpipe (docling + LLM-разметка
    этапов/подэтапов, метки в Postgres). Векторов больше нет. Статус ведём по ходу
    (reanalyzing -> indexed/error), чтобы был виден в списке документов."""
    if is_confidential(filename):
        return {"filename": filename, "error": "Документ помечен «не отправлять в ИИ»"}
    _update_registry(filename, status="reanalyzing", error=None,
                     phase="Переклассификация (docpipe)", progress=10)
    try:
        import docpipe
        fp = DOCS_DIR / filename
        # Локальный кэш мог потеряться (новый контейнер/сервер) — берём оригинал из
        # хранилища исходников (raw-БД, S3), как и при первичном разборе.
        if not storage.pull(fp, (docregistry.get(filename) or {}).get("s3_key") or ""):
            raise FileNotFoundError(f"нет оригинала {filename} ни в data/documents, ни в хранилище")
        res = docpipe.ingest(str(fp), filename=filename, force=True,
                             progress_cb=_docpipe_progress_cb(filename))
        sections = res.get("sections") or 0
        _update_registry(filename, status="indexed", chunks=sections, error=None,
                         phase=None, progress=100)
        return {"filename": filename, "sections": sections}
    except Exception as e:
        _log("REANALYZE", f"{filename}: ошибка переклассификации ({e})")
        _update_registry(filename, status="error", error=str(e))
        return {"filename": filename, "error": str(e)}


def reanalyze_all(job_id: str = None) -> dict:
    """Полный повторный анализ всей базы (ТЗ §8 — «запустить повторный анализ всей базы»).

    job_id — если задан, прогресс ПАКЕТА (документ i из N + имя текущего) пишется в стор
    задач, фронт тянет его через GET /documents/jobs/{job_id} и рисует общий прогресс.
    Прогресс ВНУТРИ документа пишет reanalyze_document в реестр (бар на карточке).
    Векторов/папок больше нет — переклассификация идёт через docpipe (LLM)."""
    todo = [d["filename"] for d in list_documents() if d.get("status") == "indexed"]
    total = len(todo)
    if job_id:
        _set_index_job(job_id, status="processing", done=0, total=total, current="",
                       started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    results = []
    try:
        for i, filename in enumerate(todo):
            if job_id:
                _set_index_job(job_id, done=i, total=total, current=filename)
            results.append(reanalyze_document(filename))
        if job_id:
            _set_index_job(job_id, status="done", done=total, total=total, current="")
        docs_changed()
        return {"reanalyzed": len(results), "documents": results}
    except Exception as e:
        if job_id:
            _set_index_job(job_id, status="error", error=str(e))
        raise
