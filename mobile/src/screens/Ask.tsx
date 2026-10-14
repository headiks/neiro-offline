// «Вопрос»: сегмент «Диалог / Мои вопросы · N». Диалог — вопрос справа (акцентный пузырь),
// ответ текстом (без источников) или бейджем «Передано специалисту», подсказки-чипы и
// композер-пилюля с круглой кнопкой ↑ над таб-баром.
import React, { useState } from "react";
import { ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ADDRESSEE, QUESTION_STATUS } from "../../../shared/status";
import { ago } from "../../../shared/format";
import { SUGGESTIONS } from "../api";
import { useData, type Turn } from "../data";
import { font, S, useTheme } from "../theme";
import { Badge, Button, Chip, Glass, GlassCard, ScreenTitle, Segmented, Txt } from "../ui";
import { ArrowUpIcon, RetryIcon } from "../icons";
import { Screen, TABBAR_SPACE } from "./Screen";

function AnswerTurn({ t, onRetry }: { t: Turn; onRetry: () => void }) {
  const { c } = useTheme();
  return (
    <View style={{ gap: S.sm }}>
      <View style={{ alignSelf: "flex-end", maxWidth: "85%", backgroundColor: c.primary, paddingHorizontal: 14, paddingVertical: 10,
                     borderRadius: S.r, borderBottomRightRadius: S.rSm }}>
        <Txt color={c.primaryText}>{t.q}</Txt>
      </View>
      <View style={{ alignSelf: "flex-start", maxWidth: "92%", gap: S.sm }}>
        {t.status === "pending" ? (
          <View style={{ flexDirection: "row", gap: S.sm, alignItems: "center" }}>
            <ActivityIndicator color={c.primary} /><Txt v="small" color={c.muted}>Ищу ответ…</Txt>
          </View>
        ) : t.status === "error" ? (
          <>
            <Badge tone="danger">Ошибка</Badge>
            <Txt v="small" color={c.muted}>{t.a}</Txt>
            <Button title="Повторить" variant="secondary" icon={<RetryIcon color={c.text} size={17} />} onPress={onRetry} style={{ alignSelf: "flex-start" }} />
          </>
        ) : (
          <>
            <Txt>{t.a}</Txt>
            {t.escalated ? <Badge tone="warn">Передано специалисту</Badge> : null}
          </>
        )}
      </View>
    </View>
  );
}

function Composer() {
  const { c, fonts } = useTheme();
  const { ask, asking } = useData();
  const [q, setQ] = useState("");
  const send = () => { if (!q.trim() || asking) return; ask(q); setQ(""); };
  const off = !q.trim() || asking;
  return (
    <Glass radius={S.pill} fill={c.tabBar}>
      <View style={{ flexDirection: "row", alignItems: "center", paddingLeft: 18, paddingRight: 5, paddingVertical: 5, gap: S.sm }}>
        <TextInput value={q} onChangeText={setQ} placeholder="Ваш вопрос…" placeholderTextColor={c.muted} maxLength={2000}
                   accessibilityLabel="Ваш вопрос" onSubmitEditing={send} returnKeyType="send" submitBehavior="submit"
                   style={[{ flex: 1, minHeight: 40, color: c.text, fontSize: 15 }, font("400", fonts)]} />
        <Pressable accessibilityRole="button" accessibilityLabel="Отправить" disabled={off} onPress={send}
                   style={{ width: 40, height: 40, borderRadius: 20, backgroundColor: c.primary, alignItems: "center", justifyContent: "center", opacity: off ? 0.45 : 1 }}>
          <ArrowUpIcon color={c.primaryText} size={20} strokeWidth={2.4} />
        </Pressable>
      </View>
    </Glass>
  );
}

function MyQuestions() {
  const { c } = useTheme();
  const { questions } = useData();
  if (!questions.length) {
    return <GlassCard><Txt v="small" color={c.muted}>Вопросов специалисту пока нет. Если ассистент не найдёт ответ в документах, вопрос появится здесь, а ответ придёт сюда же.</Txt></GlassCard>;
  }
  return (
    <>
      {questions.map((q) => (
        <GlassCard key={q.id}>
          <View style={{ flexDirection: "row", gap: S.sm, justifyContent: "space-between", alignItems: "flex-start" }}>
            <Txt w="600" style={{ flex: 1 }}>{q.addressee && ADDRESSEE[q.addressee] ? `${ADDRESSEE[q.addressee].to}: ` : ""}{q.question}</Txt>
            <Badge tone={(QUESTION_STATUS[q.status] || QUESTION_STATUS.open).tone}>{(QUESTION_STATUS[q.status] || QUESTION_STATUS.open).label}</Badge>
          </View>
          {q.status === "resolved" && q.answer ? <Txt v="small" color={c.muted}>{q.answer}</Txt> : <Txt v="micro" color={c.muted}>Задан {ago(q.created_at)}</Txt>}
        </GlassCard>
      ))}
    </>
  );
}

export default function Ask() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const { turns, ask, asking, retry, newDialog, questions } = useData();
  const [view, setView] = useState<"dialog" | "questions">("dialog");
  const open = questions.filter((q) => q.status !== "resolved").length;
  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Screen stickToBottom footerSpace={view === "dialog" ? TABBAR_SPACE + 64 : TABBAR_SPACE}>
        <ScreenTitle title="Вопрос" />
        <Segmented value={view} onChange={setView}
                   options={[{ value: "dialog", label: "Диалог" }, { value: "questions", label: `Мои вопросы${questions.length ? ` · ${open || questions.length}` : ""}` }]} />
        {view === "questions" ? <MyQuestions /> : (
          <>
            {!turns.length ? (
              <>
                <Txt v="small" color={c.muted}>Спросите про работу, адаптацию или порядки в компании. Если я не знаю ответа — передам вопрос специалисту.</Txt>
                <View style={{ flexDirection: "row", flexWrap: "wrap", gap: S.sm }}>
                  {SUGGESTIONS.map((s) => <Chip key={s} label={s} onPress={() => ask(s)} disabled={asking} />)}
                </View>
              </>
            ) : turns.map((t) => <AnswerTurn key={t.id} t={t} onRetry={() => retry(t)} />)}
            {turns.length > 0 ? <Button title="Новый диалог" variant="ghost" onPress={newDialog} disabled={asking} style={{ alignSelf: "flex-start" }} /> : null}
          </>
        )}
      </Screen>
      {view === "dialog" ? (
        <View style={{ position: "absolute", left: 14, right: 14, bottom: insets.bottom + 22 + 72 }}><Composer /></View>
      ) : null}
    </KeyboardAvoidingView>
  );
}
