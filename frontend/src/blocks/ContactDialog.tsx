// «Написать наставнику / руководителю / в отдел кадров» из сообщения плана: вопрос встаёт в
// очередь администратору с адресатом, ответ приходит в «Мои вопросы».
import { useState } from 'react';
import type { ContactRole } from '@shared/types';
import { ADDRESSEE } from '@shared/status';
import { api, messageOf } from '../lib/api';
import { useCabinet } from '../lib/cabinet';
import { useMe } from '../lib/me';
import { useToast } from '../lib/toast';
import { Button, Textarea } from '../ui';
import { Dialog } from '../ui/Dialog';

export function ContactDialog({ role, onClose }: { role: ContactRole | null; onClose: () => void }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const { reloadQuestions } = useCabinet();
  const me = useMe().me;
  const toast = useToast();
  if (!role) return null;
  const who = role === 'mentor' ? me?.mentor : role === 'manager' ? me?.manager : '';
  const close = () => { setText(''); onClose(); };
  const send = async () => {
    setBusy(true);
    try {
      await api.askPerson(role, text.trim());
      toast.ok('Вопрос отправлен — ответ придёт в «Мои вопросы».');
      reloadQuestions();
      close();
    } catch (e) {
      toast.error(messageOf(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open onClose={close} title={ADDRESSEE[role].title} subtitle={who || undefined}
            footer={<>
              <Button variant="ghost" onClick={close}>Отмена</Button>
              <Button variant="primary" loading={busy} disabled={!text.trim()} onClick={send}>Отправить</Button>
            </>}>
      <Textarea autosize autoFocus value={text} maxLength={2000} placeholder="Ваш вопрос"
                onChange={(e) => setText(e.target.value)} aria-label="Ваш вопрос" />
      <div className="nm-micro nm-muted">Ответ придёт в «Мои вопросы».</div>
    </Dialog>
  );
}
