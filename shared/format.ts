// Даты, приветствие и подписи — одинаково на сайте и в приложении.
import type { Msg } from "./types";

const pad = (n: number) => String(n).padStart(2, "0");

const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
  "августа", "сентября", "октября", "ноября", "декабря"];
const MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const WEEKDAYS = ["Воскресенье", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"];
const WEEKDAYS_SHORT = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];

/** Строка времени сервера -> Date (сервер отдаёт UTC с зоной или «YYYY-MM-DD HH:MM»). */
export function parseDate(raw?: string | null): Date | null {
  if (!raw) return null;
  const d = new Date(String(raw).replace(" ", "T"));
  return isNaN(d.getTime()) ? null : d;
}

/** Локальная дата YYYY-MM-DD. */
export function ymd(d: Date = new Date()): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Дата «YYYY-MM-DD» без часового пояса -> локальная полночь. */
export function fromYmd(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

export function hhmmOf(d: Date | null): string {
  return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : "";
}

/** Момент попадания сообщения в инбокс (для чата важна доставка, а не план). */
export function msgDate(m: Msg): Date | null {
  return parseDate(m.delivered_at || m.send_at);
}

export const msgDay = (m: Msg) => { const d = msgDate(m); return d ? ymd(d) : ""; };
export const msgTime = (m: Msg) => hhmmOf(msgDate(m));
export const msgTs = (m: Msg) => msgDate(m)?.getTime() || 0;

/** Старые сверху, новые снизу — как в мессенджере. Внутри дня — строго по плану (время
 *  по плану, затем порядок этапа и подэтапа): пачка, доставленная разом, шла вперемешку. */
export function oldestFirst(list: Msg[]): Msg[] {
  const planTs = (m: Msg) => parseDate(m.send_at)?.getTime() || 0;
  return list.slice().sort((a, b) => msgDay(a).localeCompare(msgDay(b))
    || planTs(a) - planTs(b) || (a.seq ?? 0) - (b.seq ?? 0) || msgTs(a) - msgTs(b));
}

/** Заголовок сообщения «Этап — Подэтап» -> kicker «Этап · Подэтап». */
export function msgKicker(m: Msg): string {
  return String(m.title || "Сообщение плана").split(" — ").filter(Boolean).join(" · ");
}

/** «Сегодня» / «Вчера» / «12 сен 2026» — подпись дня в истории. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  if (iso === ymd(now)) return "Сегодня";
  if (iso === ymd(new Date(now.getTime() - 86400000))) return "Вчера";
  const d = fromYmd(iso);
  if (!d) return iso;
  const year = d.getFullYear() === now.getFullYear() ? "" : ` ${d.getFullYear()}`;
  return `${d.getDate()} ${MONTHS_GEN[d.getMonth()]}${year}`;
}

/** «Среда, 24 сентября». */
export function longDate(d: Date = new Date()): string {
  return `${WEEKDAYS[d.getDay()]}, ${d.getDate()} ${MONTHS_GEN[d.getMonth()]}`;
}

/** «Пт, 26 сен · 10:00» — когда придёт следующее сообщение плана. */
export function shortWhen(d: Date | null, now: Date = new Date()): string {
  if (!d) return "";
  const t = hhmmOf(d);
  if (ymd(d) === ymd(now)) return `сегодня, ${t}`;
  if (ymd(d) === ymd(new Date(now.getTime() + 86400000))) return `завтра, ${t}`;
  return `${WEEKDAYS_SHORT[d.getDay()]}, ${d.getDate()} ${MONTHS_SHORT[d.getMonth()]} · ${t}`;
}

/** «22.09.2026». */
export function ruDate(iso?: string | null): string {
  const d = iso ? fromYmd(iso) : null;
  return d ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}` : "—";
}

/** «24.09.2026, 14:05» — для таблиц и журналов. */
export function ruDateTime(raw?: string | null): string {
  const d = parseDate(raw);
  if (!d) return raw ? String(raw) : "—";
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}, ${hhmmOf(d)}`;
}

/** «5 мин назад», «вчера», «12 сен» — время вопроса в очереди. */
export function ago(raw?: string | null, now: Date = new Date()): string {
  const d = parseDate(raw);
  if (!d) return "";
  const min = Math.round((now.getTime() - d.getTime()) / 60000);
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  if (ymd(d) === ymd(now)) return `сегодня, ${hhmmOf(d)}`;
  if (ymd(d) === ymd(new Date(now.getTime() - 86400000))) return `вчера, ${hhmmOf(d)}`;
  return `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]}`;
}

export function greeting(now: Date = new Date()): string {
  const h = now.getHours();
  if (h >= 5 && h < 12) return "Доброе утро";
  if (h >= 12 && h < 18) return "Добрый день";
  if (h >= 18 && h < 23) return "Добрый вечер";
  return "Доброй ночи";
}

/** «Иванов Иван Иванович» -> «Иван» (как плейсхолдер [Имя] в сообщениях плана). */
export function firstName(fullName?: string | null): string {
  const parts = String(fullName || "").split(/\s+/).filter(Boolean);
  return parts.length >= 2 ? parts[1] : parts[0] || "";
}

/** Инициалы для аватара: «Котова Марина» -> «МК» (имя, фамилия). */
export function initials(fullName?: string | null, fallback = "?"): string {
  const parts = String(fullName || "").split(/\s+/).filter(Boolean);
  if (!parts.length) return fallback;
  const pick = parts.length >= 2 ? [parts[1], parts[0]] : [parts[0]];
  return pick.map((p) => p[0]).join("").toUpperCase();
}

/** 1 документ, 2 документа, 5 документов. */
export function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

/** Имя файла-источника -> подпись: без расширения и подчёркиваний. */
export function sourceLabel(name: string): string {
  return String(name || "").replace(/\.[a-z0-9]{2,5}$/i, "").replace(/[_]+/g, " ").trim();
}
