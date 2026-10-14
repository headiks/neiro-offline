// Прогресс адаптации и итоги интерактивных сообщений — одна логика для сайта и приложения.
import { fromYmd, parseDate, plural, shortWhen, ymd } from "./format";
import type { Answers, Msg, MySchedule, Payload, QuizQuestion, ScheduleItem } from "./types";

const DAY = 86400000;

export type Progress = {
  day: number;                    // какой сейчас день плана (0 — план ещё не начался)
  total: number;                  // сколько дней в плане
  ratio: number;                  // 0..1 для полосы прогресса
  stage: string;                  // текущий этап
  started: boolean;
  finished: boolean;
  paused: boolean;                // на больничном: план стоит, дни не идут
  next: { title: string; stage: string; substage: string; at: Date } | null;
  upcoming: { title: string; stage: string; substage: string; brief: string; at: Date }[];
};

const itemAt = (it: ScheduleItem) => parseDate(it.schedule?.send_at);
const plannedAt = (it: ScheduleItem) => parseDate(it.schedule?.planned_at || it.schedule?.send_at);

/** Сколько мс плана (от from до now) пришлось на больничные — эти дни план стоял. */
function pausedMs(s: MySchedule, from: Date, now: Date): number {
  return (s.pauses || []).reduce((sum, p) => {
    const a = parseDate(p.start), b = p.end ? parseDate(p.end) : now;
    if (!a || !b) return sum;
    const lo = Math.max(a.getTime(), from.getTime()), hi = Math.min(b.getTime(), now.getTime());
    return sum + Math.max(0, hi - lo);
  }, 0);
}

/** «День 3 из 30», текущий этап и что дальше — по расписанию из /api/my/schedule. */
export function adaptationProgress(s: MySchedule | null | undefined, now: Date = new Date()): Progress | null {
  if (!s || !Array.isArray(s.messages)) return null;
  const start = fromYmd(s.start_date);
  const dated = s.messages.map((it) => ({ it, at: itemAt(it) })).filter((x) => x.at) as
    { it: ScheduleItem; at: Date }[];
  dated.sort((a, b) => a.at.getTime() - b.at.getTime());
  if (!start) return null;
  const today = fromYmd(ymd(now)) as Date;
  // Длина плана — по исходным датам; дни больничного не считаются: план в это время стоит.
  const planned = s.messages.map(plannedAt).filter(Boolean) as Date[];
  const lastPlanned = planned.length ? new Date(Math.max(...planned.map((d) => d.getTime()))) : start;
  const lastDay = fromYmd(ymd(lastPlanned)) as Date;
  const total = Math.max(1, Math.round((lastDay.getTime() - start.getTime()) / DAY) + 1);
  const raw = Math.floor((today.getTime() - start.getTime()) / DAY) + 1 - Math.round(pausedMs(s, start, now) / DAY);
  const started = raw >= 1;
  const day = Math.min(Math.max(raw, 0), total);
  const past = dated.filter((x) => x.at.getTime() <= now.getTime());
  const future = dated.filter((x) => x.at.getTime() > now.getTime());
  const current = past.length ? past[past.length - 1].it : dated[0]?.it;
  const view = (x: { it: ScheduleItem; at: Date }) => ({
    title: [x.it.stage?.title, x.it.substage?.title].filter(Boolean).join(" · "),
    stage: x.it.stage?.title || "",
    substage: x.it.substage?.title || "",
    brief: x.it.substage?.brief || "",
    at: x.at,
  });
  return {
    day,
    total,
    ratio: Math.min(1, Math.max(0, day / total)),
    stage: current?.stage?.title || "",
    started,
    finished: started && !future.length && raw > total,
    paused: !!s.paused,
    next: future.length ? view(future[0]) : null,
    upcoming: future.slice(0, 6).map(view),
  };
}

/** Заголовок карточки прогресса: «До выхода 3 дня», «День 5 из 30», «Пауза · день 5 из 30». */
export function progressTitle(p: Progress, s: MySchedule, now: Date = new Date()): string {
  if (!p.started) {
    const start = fromYmd(s.start_date);
    const left = start ? Math.ceil((start.getTime() - now.getTime()) / DAY) : 0;
    return left > 0 ? `До выхода ${left} ${plural(left, "день", "дня", "дней")}` : "Скоро старт";
  }
  if (p.finished) return "План пройден";
  return p.paused ? `Пауза · день ${p.day} из ${p.total}` : `День ${p.day} из ${p.total}`;
}

/** Когда придёт следующее. На больничном дат нет: план стоит до выхода. */
export function whenNext(p: Progress, at: Date): string {
  return p.paused ? "после выхода с больничного" : shortWhen(at);
}

/** «Дальше: Знакомство с наставником — завтра в 10:00». */
export function nextLine(p: Progress): string {
  return p.next ? `Дальше: ${p.next.substage || p.next.title} — ${whenNext(p, p.next.at)}` : "";
}

// ---- Интерактивные сообщения ----
export type MsgStats =
  | { kind: "checklist"; done: number; total: number; label: string }
  | { kind: "quiz"; answered: number; total: number; right: number; label: string }
  | { kind: "survey"; answered: number; total: number; label: string }
  | { kind: "text"; label: "" };

export function isChecklist(p?: Payload | null): p is { type: "checklist"; intro?: string; items: { id: string; text: string }[] } {
  return !!p && p.type === "checklist" && Array.isArray((p as any).items);
}

export function isQuestions(p?: Payload | null): p is { type: "survey" | "quiz"; intro?: string; questions: QuizQuestion[] } {
  return !!p && (p.type === "survey" || p.type === "quiz") && Array.isArray((p as any).questions);
}

export function isRight(q: QuizQuestion, answers: Answers): boolean {
  return (q.options || []).some((o) => o.correct && answers[q.id] === o.id);
}

/** Итог под карточкой: «Выполнено: 1 из 4», «Результат: 2 из 3», «Спасибо, ответы отправлены». */
export function msgStats(m: Msg): MsgStats {
  const p = m.payload, a = m.answers || {};
  if (isChecklist(p)) {
    const done = p.items.filter((it) => a[it.id]).length;
    return { kind: "checklist", done, total: p.items.length, label: `Выполнено: ${done} из ${p.items.length}` };
  }
  if (isQuestions(p)) {
    const total = p.questions.length;
    const answered = p.questions.filter((q) => a[q.id]).length;
    if (p.type === "quiz") {
      const right = p.questions.filter((q) => isRight(q, a)).length;
      return { kind: "quiz", answered, total, right,
               label: answered === total ? `Результат: ${right} из ${total}` : answered ? `Отвечено: ${answered} из ${total}` : "Выберите вариант" };
    }
    return { kind: "survey", answered, total,
             label: answered === total ? "Спасибо, ответы отправлены" : `Отвечено: ${answered} из ${total}` };
  }
  return { kind: "text", label: "" };
}

/** Новый набор ответов: value=null — переключить пункт чек-листа, иначе выбранный вариант.
 *  В мини-тесте ответ не меняется после выбора (варианты блокируются). */
export function applyAnswer(m: Msg, key: string, value: string | null): Answers | null {
  const a: Answers = { ...(m.answers || {}) };
  if (value === null) a[key] = !a[key];
  else {
    if (m.payload?.type === "quiz" && a[key]) return null;
    const q = isQuestions(m.payload) ? m.payload.questions.find((x) => x.id === key) : undefined;
    // Несколько вариантов: повторное нажатие снимает выбор, ответ — id через запятую.
    a[key] = q?.type === "multi" ? toggleId(String(a[key] || ""), value) : value;
  }
  return a;
}

/** «o1,o3» + «o3» -> «o1»; + «o2» -> «o1,o3,o2». */
export function toggleId(list: string, id: string): string {
  const ids = list.split(",").filter(Boolean);
  return (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]).join(",");
}

/** Выбран ли вариант (у мультивыбора ответ — id через запятую). */
export const isPicked = (answer: unknown, id: string) => String(answer || "").split(",").includes(id);
