// «Написать наставнику / руководителю / в отдел кадров» из сообщения плана: вопрос встаёт в
// очередь администратору с адресатом, ответ приходит в «Мои вопросы» (как на сайте).
import React, { useState } from "react";
import { Alert, KeyboardAvoidingView, Modal, Platform, TextInput, View } from "react-native";
import type { ContactRole } from "../../../shared/types";
import { ADDRESSEE } from "../../../shared/status";
import { api } from "../api";
import { useData } from "../data";
import { font, S, useTheme } from "../theme";
import { Button, Glass, Txt } from "../ui";

export default function ContactSheet({ role, onClose }: { role: ContactRole | null; onClose: () => void }) {
  const { c, fonts } = useTheme();
  const { me, reloadQuestions } = useData();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  if (!role) return null;
  const who = role === "mentor" ? me?.mentor : role === "manager" ? me?.manager : "";
  const close = () => { setText(""); onClose(); };
  const send = async () => {
    setBusy(true);
    try {
      await api.askPerson(role, text.trim());
      reloadQuestions();
      close();
      Alert.alert("Вопрос отправлен", "Ответ придёт в «Мои вопросы».");
    } catch (e) {
      Alert.alert("Не отправлено", e instanceof Error ? e.message : "Проверьте связь и попробуйте ещё раз.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal visible transparent animationType="fade" onRequestClose={close}>
      <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined}
                            style={{ flex: 1, justifyContent: "flex-end", padding: S.lg, paddingBottom: 40, backgroundColor: c.backdrop }}>
        <Glass fill={c.bg}>
          <View style={{ padding: S.xl, gap: S.md }}>
            <Txt v="h2">{ADDRESSEE[role].title}</Txt>
            {who ? <Txt v="small" color={c.muted}>{who}</Txt> : null}
            <TextInput value={text} onChangeText={setText} placeholder="Ваш вопрос" placeholderTextColor={c.muted}
                       maxLength={2000} multiline autoFocus accessibilityLabel="Ваш вопрос"
                       style={[{ minHeight: 96, color: c.text, fontSize: 15, borderWidth: 1, borderColor: c.border,
                                 borderRadius: S.rSm, padding: S.md, textAlignVertical: "top" }, font("400", fonts)]} />
            <Txt v="micro" color={c.muted}>Ответ придёт в «Мои вопросы».</Txt>
            <View style={{ flexDirection: "row", gap: S.sm, justifyContent: "flex-end" }}>
              <Button title="Отмена" variant="ghost" onPress={close} />
              <Button title="Отправить" loading={busy} disabled={!text.trim()} onPress={send} />
            </View>
          </View>
        </Glass>
      </KeyboardAvoidingView>
    </Modal>
  );
}
