// «Мои вопросы специалисту»: вопрос, статус (Ждёт ответа / Отвечено), ответ.
import { MessageCircleQuestion } from 'lucide-react';
import { ADDRESSEE, QUESTION_STATUS } from '@shared/status';
import { ago } from '@shared/format';
import { useCabinet } from '../lib/cabinet';
import { Card, Empty, Spinner, StatusBadge } from '../ui';

export function MyQuestions() {
  const { questions, questionsLoaded } = useCabinet();
  if (!questionsLoaded) return <Card pad={false} data-tour="my-questions"><Spinner /></Card>;
  if (!questions.length) {
    return <Card pad={false} data-tour="my-questions"><Empty icon={MessageCircleQuestion}>Вопросов специалисту пока нет. Если ассистент не найдёт ответ, вопрос появится здесь, а ответ придёт сюда же.</Empty></Card>;
  }
  return (
    <Card pad={false} data-tour="my-questions">
      {questions.map((q) => (
        <div className="nm-qcard" key={q.id}>
          <div className="nm-row-between">
            <div className="nm-qcard-q nm-grow">{q.addressee && ADDRESSEE[q.addressee] ? `${ADDRESSEE[q.addressee].to}: ` : ''}{q.question}</div>
            <StatusBadge view={QUESTION_STATUS[q.status] || QUESTION_STATUS.open} />
          </div>
          {q.status === 'resolved' && q.answer ? <div className="nm-qcard-a nm-pre">{q.answer}</div>
            : <div className="nm-micro nm-muted">Задан {ago(q.created_at)}</div>}
        </div>
      ))}
    </Card>
  );
}
