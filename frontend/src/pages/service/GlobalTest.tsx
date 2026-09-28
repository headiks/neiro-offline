// Диагностика: все служебные инструменты в одном месте. Раздел закрыт отдельным паролем
// (сервер: deps.require_globaltest) — без него ни страницы, ни их API недоступны.
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Bell, Database, FlaskConical, HardDrive, LayoutGrid, ListChecks, Lock, LockOpen, ScanText, ScrollText, Table2, type LucideIcon } from 'lucide-react';
import { api, messageOf } from '../../lib/api';
import { Button, Callout, Card, Field, Input, PageHeader, Spinner } from '../../ui';

const DATA: [string, LucideIcon, string, string][] = [
  ['/documents-table', Table2, 'Реестр документов', 'Таблица метаданных обработанных файлов.'],
  ['/documents-board', LayoutGrid, 'Этапы ↔ документы', 'Какие документы закреплены за этапами и подэтапами.'],
  ['/doc-breakdown', ScanText, 'Разбор документа', 'Блоки, метки этапов и обоснования разметки.'],
  ['/s3', HardDrive, 'Хранилище S3', 'Обозреватель оригиналов документов (только чтение).'],
  ['/plans-db', Database, 'База планов', 'Структура планов и расписаний (JSONB).'],
];
const TOOLS: [string, LucideIcon, string, string][] = [
  ['/notify-test', Bell, 'Тест уведомлений', 'Отправка сообщений пользователю, проверка пушей.'],
  ['/message-test', FlaskConical, 'Тестовые сообщения', 'Сообщения всех типов: чек-лист, опрос, мини-тест.'],
  ['/queue-test', ListChecks, 'Очереди (RQ/Redis)', 'Статус воркеров, длина очереди, тест-задача.'],
  ['/logs', ScrollText, 'Журнал действий', 'Действия пользователей (по отделам / все).'],
];

function Grid({ items }: { items: typeof DATA }) {
  return (
    <div className="nm-grid-3">
      {items.map(([to, Icon, title, text]) => (
        <Link key={to} to={to} className="nm-tool nm-glass"><Icon aria-hidden /><span><b>{title}</b><span>{text}</span></span></Link>
      ))}
    </div>
  );
}

function Unlock({ configured, onDone }: { configured: boolean; onDone: () => void }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true); setError(null);
    try { await api.post('/api/globaltest/unlock', { password }); onDone(); }
    catch (e) { setError(messageOf(e)); setBusy(false); }
  };
  return (
    <Card style={{ maxWidth: 420 }}>
      <form className="nm-stack" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <div className="nm-row" style={{ gap: 8 }}><Lock aria-hidden /><b>Раздел закрыт паролем</b></div>
        {!configured && <Callout tone="warn">Пароль раздела не настроен на сервере.</Callout>}
        {error && <Callout tone="danger">{error}</Callout>}
        <Field label="Пароль раздела">
          <Input type="password" autoComplete="off" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" icon={LockOpen} loading={busy} disabled={!password}>Открыть</Button>
      </form>
    </Card>
  );
}

export default function GlobalTest() {
  const [state, setState] = useState<{ configured: boolean; unlocked: boolean } | null>(null);
  const load = () => api.get<{ configured: boolean; unlocked: boolean }>('/api/globaltest').then(setState).catch(() => setState({ configured: false, unlocked: false }));
  useEffect(() => { document.title = 'Диагностика · НейроМастер'; load(); }, []);
  const lock = () => api.post('/api/globaltest/lock').finally(load);
  if (!state) return <div className="nm-page"><PageHeader title="Диагностика" /><Spinner /></div>;
  if (!state.unlocked) return <div className="nm-page"><PageHeader title="Диагностика" subtitle="Тесты и просмотр внутренних данных." /><Unlock configured={state.configured} onDone={load} /></div>;
  return (
    <div className="nm-page">
      <PageHeader title="Диагностика" subtitle="Служебные инструменты: тесты и просмотр внутренних данных. Вынесены сюда, чтобы не мешать основной работе."
                  actions={<Button icon={Lock} onClick={lock}>Закрыть раздел</Button>} />
      <div className="nm-section-label">Данные и документы</div>
      <Grid items={DATA} />
      <div className="nm-section-label">Диагностика и тесты</div>
      <Grid items={TOOLS} />
    </div>
  );
}
