// Карточка сообщения плана (COMPONENTS.md → MessageCard): «Этап · Подэтап» + время и тело
// по типу: текст, чек-лист, мини-тест (верно/неверно + пояснение), опрос. Ответ сохраняется
// сразу и виден на сайте — логика общая (shared/progress.ts). Под сообщением — кнопки
// «Написать наставнику / руководителю / в отдел кадров», если текст к ним отсылает.
import React, { useState } from "react";
import { Pressable, TextInput, View } from "react-native";
import type { ContactRole, Msg } from "../../../shared/types";
import { msgKicker, msgTime } from "../../../shared/format";
import { isChecklist, isPicked, isQuestions, msgStats } from "../../../shared/progress";
import { font, S, useTheme } from "../theme";
import { Button, GlassCard, Progress, Txt } from "../ui";
import { CheckIcon } from "../icons";
import ContactSheet from "./ContactSheet";

export default function MessageCard({ m, onAnswer }: { m: Msg; onAnswer: (m: Msg, key: string, value: string | null) => void }) {
  const { c, fonts } = useTheme();
  const [contact, setContact] = useState<ContactRole | null>(null);
  const p = m.payload, a = m.answers || {};
  const stats = msgStats(m);
  const intro = (p as { intro?: string } | null)?.intro;
  let body: React.ReactNode = null;
  if (isChecklist(p)) {
    body = (
      <>
        {intro ? <Txt>{intro}</Txt> : null}
        <Txt v="small" color={c.muted}>Отмечайте пункты по мере выполнения.</Txt>
        {p.items.map((it) => {
          const on = !!a[it.id];
          return (
            <Pressable key={it.id} accessibilityRole="checkbox" accessibilityState={{ checked: on }} onPress={() => onAnswer(m, it.id, null)}
                       style={{ flexDirection: "row", alignItems: "center", gap: S.md, minHeight: 44, paddingVertical: 4 }}>
              <View style={{ width: 22, height: 22, borderRadius: 7, borderWidth: 2, alignItems: "center", justifyContent: "center",
                             borderColor: on ? c.primary : c.lineStrong, backgroundColor: on ? c.primary : "transparent" }}>
                {on ? <CheckIcon color={c.primaryText} size={14} strokeWidth={3} /> : null}
              </View>
              <Txt style={{ flex: 1, textDecorationLine: on ? "line-through" : "none" }} color={on ? c.muted : c.text}>{it.text}</Txt>
            </Pressable>
          );
        })}
      </>
    );
  } else if (isQuestions(p)) {
    const quiz = p.type === "quiz";
    body = (
      <>
        {intro ? <Txt>{intro}</Txt> : null}
        {p.questions.map((q, qi) => {
          const chosen = a[q.id] as string | undefined;
          const multi = q.type === "multi";
          // Короткие варианты (шкала 1–5, да/нет) — в ряд, длинные — столбиком.
          const row = (q.options || []).every((o) => o.text.length <= 12);
          return (
            <View key={q.id} style={{ gap: S.sm, marginTop: qi ? S.sm : 0 }}>
              <Txt w="600">{p.questions.length > 1 ? `${qi + 1}. ` : ""}{q.text}</Txt>
              {multi ? <Txt v="small" color={c.muted}>Можно выбрать несколько.</Txt> : null}
              {!q.options?.length && !quiz ? (
                <TextInput defaultValue={chosen || ""} placeholder="Ваш ответ" placeholderTextColor={c.muted} maxLength={1000}
                           multiline accessibilityLabel={q.text}
                           onEndEditing={(e) => { const v = e.nativeEvent.text.trim(); if (v !== (chosen || "")) onAnswer(m, q.id, v); }}
                           style={[{ minHeight: 44, color: c.text, fontSize: 15, borderWidth: 1, borderColor: c.border,
                                     borderRadius: S.rSm, paddingHorizontal: 14, paddingVertical: 10 }, font("400", fonts)]} />
              ) : (
                <View style={row ? { flexDirection: "row", flexWrap: "wrap", gap: S.sm } : { gap: S.sm }}>
                  {(q.options || []).map((o) => {
                    const picked = multi ? isPicked(chosen, o.id) : chosen === o.id;
                    const state = quiz && chosen ? (o.correct ? "right" : picked ? "wrong" : null) : null;
                    const border = state === "right" ? c.ok : state === "wrong" ? c.danger : picked ? c.primary : c.border;
                    const bg = state === "right" ? c.okSoft : state === "wrong" ? c.dangerSoft : picked ? c.soft : "transparent";
                    return (
                      <Pressable key={o.id} accessibilityRole="button" accessibilityState={{ selected: picked, disabled: quiz && !!chosen }}
                                 disabled={quiz && !!chosen} onPress={() => { onAnswer(m, q.id, o.id); if (o.contact) setContact(o.contact); }}
                                 style={{ minHeight: 44, minWidth: row ? 48 : undefined, alignItems: row ? "center" : undefined,
                                          justifyContent: "center", paddingHorizontal: 14, paddingVertical: 10,
                                          borderRadius: S.rSm, borderWidth: 1.5, borderColor: border, backgroundColor: bg }}>
                        <Txt>{o.text}</Txt>
                      </Pressable>
                    );
                  })}
                </View>
              )}
              {quiz && chosen && q.explanation ? <Txt v="small" color={c.muted}>{q.explanation}</Txt> : null}
            </View>
          );
        })}
      </>
    );
  } else if (m.body) {
    body = <Txt>{m.body}</Txt>;
  }
  const ratio = stats.kind === "checklist" ? stats.done / (stats.total || 1)
    : stats.kind === "quiz" || stats.kind === "survey" ? stats.answered / (stats.total || 1) : 0;
  const contacts = p?.contacts || [];
  return (
    <GlassCard>
      <View style={{ flexDirection: "row", justifyContent: "space-between", gap: S.md }}>
        <Txt v="small" w="600" color={c.softInk} style={{ flex: 1 }}>{msgKicker(m)}</Txt>
        <Txt v="micro" color={c.muted}>{msgTime(m)}</Txt>
      </View>
      {body}
      {contacts.length ? (
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: S.sm }}>
          {contacts.map((ct) => <Button key={ct.role} title={ct.label} variant="secondary" onPress={() => setContact(ct.role)} />)}
        </View>
      ) : null}
      {stats.label ? (
        <View style={{ flexDirection: "row", alignItems: "center", gap: S.md, marginTop: 2 }}>
          {stats.kind !== "text" ? <View style={{ width: 48 }}><Progress value={ratio} tone="ok" height={4} /></View> : null}
          <Txt v="micro" color={c.muted}>{stats.label}</Txt>
        </View>
      ) : null}
      <ContactSheet role={contact} onClose={() => setContact(null)} />
    </GlassCard>
  );
}
