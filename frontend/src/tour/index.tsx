// Сценарии тура «Как пользоваться» для нового интерфейса. Каждый шаг сам приводит страницу
// в нужное состояние (раздел, демо-окно) — шаги можно листать в любом порядке и быстро.
// Элементы ищутся по data-tour, страницы отдают туру свои действия через useTourHooks.
import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { maybeOffer, start, stop, type Kit, type Scenario, type Step } from './engine';

type Hooks = {
  users?: { setStaffing: (on: boolean) => void; openDemo: () => void; closeDemo: () => void };
  plans?: { ready: () => boolean; showDemo: () => void; demoActive: () => boolean; snapshot: () => unknown; restore: (s: unknown) => void };
  cabinet?: { setAskView: (v: 'dialog' | 'questions') => void };
};
const hooks: Hooks = {};
// Снимок редактора планов для возврата после тура: если тур закончился на другой странице,
// редактор восстановится, когда страница планов снова откроется.
let pendingPlans: unknown;

/** Страница отдаёт туру свои действия (обновляются на каждой отрисовке). */
export function useTourHooks<K extends keyof Hooks>(name: K, h: NonNullable<Hooks[K]>) {
  useEffect(() => {
    hooks[name] = h;
    if (name === 'plans' && pendingPlans !== undefined) {
      const snap = pendingPlans;
      pendingPlans = undefined;
      (h as NonNullable<Hooks['plans']>).restore(snap);
    }
    return () => { if (hooks[name] === h) delete hooks[name]; };
  });
}

let navigate: (to: string) => void = (to) => location.assign(to);
const here = () => location.pathname + location.search;

async function go(k: Kit, path: string) {
  if (here() !== path) {
    navigate(path);
    await k.sleep(250);
  }
}

const sel = (id: string) => `[data-tour="${id}"]`;

// ---------------------------------------------------------------- админка
async function ui(k: Kit, path: string, opts: { dialog?: boolean; staffing?: boolean; demo?: boolean } = {}) {
  await go(k, path);
  if (path === '/admin/users') {
    await k.waitFor(() => (hooks.users ? document.body : null), 4000);
    if (!opts.dialog) hooks.users?.closeDemo();
    hooks.users?.setStaffing(!!opts.staffing);
    if (opts.dialog) { hooks.users?.openDemo(); await k.sleep(200); }
  }
  if (path === '/admin/plans') {
    await k.waitFor(() => (hooks.plans?.ready() ? document.body : null), 5000);
    if (opts.demo && !hooks.plans?.demoActive()) { hooks.plans?.showDemo(); await k.sleep(200); }
  }
}

const U = '/admin/users', P = '/admin/plans', D = '/admin/documents', M = '/admin/messages', Q = '/admin/questions';

const ADMIN_STEPS = (): Step[] => [
  { chapter: 'Знакомство', title: 'Как устроена админка',
    text: 'Работа идёт сверху вниз по разделам слева: люди → план адаптации → документы компании → сообщения сотрудникам. «Вопросы» — каждый день. Сайт сам пройдёт по всем шагам — можно поставить на паузу или листать стрелками.',
    target: sel('admin-nav'), enter: (k) => ui(k, U) },
  { chapter: 'Знакомство', title: 'Подсказка «что дальше»',
    text: 'Плашка вверху раздела говорит, какой следующий шаг не сделан, и ведёт в нужный раздел. Не знаете, что делать, — смотрите на неё.',
    target: sel('next-step'), enter: (k) => ui(k, U) },

  { chapter: 'Шаг 1 · Пользователи', title: 'Раздел «Пользователи»',
    text: 'Все, кто работает в системе: сотрудники на адаптации, наставники и администраторы. Начинают с него.',
    target: sel('nav-users'), click: true, enter: (k) => ui(k, U) },
  { chapter: 'Шаг 1 · Пользователи', title: 'Загрузка штатного расписания',
    text: 'Самый быстрый способ завести людей — загрузить штатное расписание (xlsx, xls или csv). ИИ сам найдёт ФИО, должности и подразделения, покажет таблицу для проверки, а уже заведённых пропустит.',
    target: sel('btn-staffing'), click: true, enter: (k) => ui(k, U), act: () => hooks.users?.setStaffing(true) },
  { chapter: 'Шаг 1 · Пользователи', title: 'Перетащите файл сюда',
    text: 'После нажатия «Создать» скачается Excel с логинами и временными паролями — его раздают сотрудникам. Пароль сотрудника меняет только администратор.',
    target: sel('staffing-drop'), enter: (k) => ui(k, U, { staffing: true }) },
  { chapter: 'Шаг 1 · Пользователи', title: 'Или добавьте человека вручную',
    text: 'Кнопка открывает карточку сотрудника. Логин создастся из ФИО, пароль — автоматически.',
    target: sel('btn-add-user'), click: true, enter: (k) => ui(k, U),
    act: async (k) => { hooks.users?.openDemo(); await k.sleep(250); } },
  { chapter: 'Шаг 1 · Пользователи', title: 'Карточка сотрудника',
    text: 'Достаточно ФИО. Для адаптации важны три поля: план, дата выхода и наставник — по ним строится расписание сообщений.',
    target: sel('emp-name'), enter: (k) => ui(k, U, { dialog: true }),
    act: (k) => k.type(`${sel('emp-name')} input`, 'Иванов Иван Иванович') },
  { chapter: 'Шаг 1 · Пользователи', title: 'План и дата выхода',
    text: 'Выберите план адаптации и дату выхода на работу. Статус «Ждёт выхода → Проходит адаптацию → Завершил» дальше меняется сам по датам.',
    target: sel('emp-plan'), enter: (k) => ui(k, U, { dialog: true }) },
  { chapter: 'Шаг 1 · Пользователи', title: 'Наставник и руководитель',
    text: 'Выбираются из сотрудников. Их имена ассистент подставит в сообщения, а наставник получит уведомление, если новичок уйдёт на больничный.',
    target: sel('emp-mentor'), enter: (k) => ui(k, U, { dialog: true }), leave: (k) => k.clearTyped() },
  { chapter: 'Шаг 1 · Пользователи', title: 'Фильтр и доступы',
    text: 'Список можно отфильтровать по подразделению и найти человека поиском. «Логины и пароли» выгружает доступы тех, кто ещё не входил.',
    target: sel('users-toolbar'), enter: (k) => ui(k, U) },
  { chapter: 'Шаг 1 · Пользователи', title: 'Действия с сотрудником',
    text: '«Расписание» — что и когда получит человек. «Изменить» — карточка. «Доступ» — новый временный пароль. «Приостановить» — больничный: план встаёт на паузу и после выхода продолжается с того же места.',
    target: sel('user-actions'), enter: (k) => ui(k, U) },

  { chapter: 'Шаг 2 · Планы', title: 'Конструктор плана адаптации',
    text: 'План — это расписание: какие сообщения, в какой день и час получит новичок. Сроки считаются от даты выхода, поэтому один план подходит всем.',
    target: sel('nav-plans'), click: true, enter: (k) => ui(k, P) },
  { chapter: 'Шаг 2 · Планы', title: 'С чего начать',
    text: 'Выберите «Стандартный план» — в нём уже есть все этапы адаптации с датами. Или «Создать свой план» с нуля. Сохранённые планы — в этом же списке.',
    target: sel('plan-select'), click: true, enter: (k) => ui(k, P),
    act: async (k) => { if (!hooks.plans?.demoActive()) hooks.plans?.showDemo(); await k.sleep(200); } },
  { chapter: 'Шаг 2 · Планы', title: 'Название плана',
    html: 'Для примера открыт <b>демо-план</b> — он не сохранится. Обычно план называют по должности: «Адаптация оператора».',
    target: sel('plan-title'), enter: (k) => ui(k, P, { demo: true }),
    act: (k) => k.type(`${sel('plan-title')} input`, 'Демо: адаптация оператора', 35) },
  { chapter: 'Шаг 2 · Планы', title: 'Этапы',
    text: 'Этап — период адаптации: «до выхода», «первый день», «первая неделя»… У каждого — длительность и отсчёт: до выхода на работу или от даты выхода.',
    target: sel('stage'), enter: (k) => ui(k, P, { demo: true }) },
  { chapter: 'Шаг 2 · Планы', title: 'Подэтап = одно сообщение',
    text: 'Внутри этапа — сообщения. Тип: обычное сообщение, чек-лист, опрос, мини-тест или напоминание. День и время отправки выбираются здесь же.',
    target: sel('sub'), enter: (k) => ui(k, P, { demo: true }) },
  { chapter: 'Шаг 2 · Планы', title: '«О чём сообщение» — задание для ИИ',
    text: 'Здесь пишут не готовый текст, а что сотрудник должен узнать. Сам текст ИИ напишет по документам компании. Значок «?» рядом с полями объясняет каждое поле.',
    target: sel('brief'), enter: (k) => ui(k, P, { demo: true }) },
  { chapter: 'Шаг 2 · Планы', title: 'Одна сессия в день',
    text: 'Если сообщений на один день много, их можно присылать вместе — во время первого и одним уведомлением, а не каждые час-два.',
    target: sel('group-daily'), enter: (k) => ui(k, P, { demo: true }) },
  { chapter: 'Шаг 2 · Планы', title: 'Добавить этап',
    text: 'Этапы берутся из готового каталога или создаются свои — список внизу плана.',
    target: sel('add-stage'), enter: (k) => ui(k, P, { demo: true }) },
  { chapter: 'Шаг 2 · Планы', title: 'Сохранить план',
    text: 'После сохранения переходите к документам. (В демо ничего не сохраняется — ваш план вернётся после тура.)',
    target: sel('save-plan'), click: true, enter: (k) => ui(k, P, { demo: true }) },

  { chapter: 'Шаг 3 · Документы', title: 'База знаний',
    text: 'Регламенты, инструкции, положения — всё, по чему ИИ будет писать сообщения и отвечать на вопросы сотрудников.',
    target: sel('nav-documents'), click: true, enter: (k) => ui(k, D) },
  { chapter: 'Шаг 3 · Документы', title: 'Загрузка',
    text: 'Перетащите файлы (PDF, DOCX, PPTX…). ИИ сам разнесёт каждый документ по этапам плана. Одинаковый файл второй раз не обрабатывается; при новой версии система спросит — заменить или сохранить отдельно.',
    target: sel('doc-drop'), click: true, enter: (k) => ui(k, D) },
  { chapter: 'Шаг 3 · Документы', title: 'Персональные данные и конфиденциальность',
    html: 'Перед отправкой в ИИ из текста автоматически убираются ФИО, телефоны, паспорта, реквизиты — модель их не видит. А документ вроде положения об оплате труда можно отметить <b>«Не отправлять в ИИ»</b>: он хранится, но ИИ его не читает.',
    target: sel('doc-confidential'), enter: (k) => ui(k, D) },
  { chapter: 'Шаг 3 · Документы', title: 'Хватает ли документов',
    text: 'Сводка показывает, сколько подэтапов каждого плана обеспечены документами. Где не хватает — ссылка «показать по этапам».',
    target: sel('coverage'), enter: (k) => ui(k, D) },
  { chapter: 'Шаг 3 · Документы', title: 'Детали по этапам',
    text: 'Какие документы к какому подэтапу отнесены и где пусто. Пустой подэтап = сообщение не сгенерируется, пока не догрузите документ.',
    target: sel('stage-board'), click: true, enter: (k) => ui(k, D) },
  { chapter: 'Шаг 3 · Документы', title: 'Список документов',
    text: 'Статус обработки каждого файла, «Посмотреть» — что ИИ нашёл в документе, «Удалить» — убрать его (сообщения по нему обновятся сами).',
    target: sel('doc-list'), enter: (k) => ui(k, D) },

  { chapter: 'Шаг 4 · Сообщения', title: 'Сообщения сотрудникам',
    text: 'Готовые тексты, которые получат новички по плану.',
    target: sel('nav-messages'), click: true, enter: (k) => ui(k, M) },
  { chapter: 'Шаг 4 · Сообщения', title: 'План и должность',
    text: 'Выберите план. Сообщения можно подстроить под должность — «Общие» получат все, для кого отдельных нет.',
    target: sel('pt-plan'), enter: (k) => ui(k, M) },
  { chapter: 'Шаг 4 · Сообщения', title: 'Одна кнопка',
    text: '«Обновить сообщения плана» сначала проверяет, что изменилось, и пишет только недостающее. Перед запуском покажет, сколько запросов к ИИ будет. Если всё актуально — запросов не будет вовсе.',
    target: sel('pt-generate'), click: true, enter: (k) => ui(k, M) },
  { chapter: 'Шаг 4 · Сообщения', title: 'Правка вручную',
    text: 'У каждого сообщения есть «Редактировать» и «Перегенерировать». Ручную правку автоматическое обновление не затрёт.',
    target: sel('pt-body'), enter: (k) => ui(k, M) },

  { chapter: 'Каждый день', title: 'Вопросы сотрудников',
    text: 'Если ассистент не нашёл ответа в документах или вопрос похож на ЧС, он попадает сюда (ЧС — сверху). Ваш ответ придёт сотруднику в кабинет и пушем. Число открытых вопросов — рядом с разделом.',
    target: sel('nav-questions'), click: true, enter: (k) => ui(k, Q) },
  { chapter: 'Каждый день', title: 'Как видит сотрудник',
    text: '«Мой кабинет» — то же, что видит сотрудник: сообщения плана, прогресс адаптации и ассистент.',
    target: sel('nav-cabinet'), enter: (k) => ui(k, Q) },
  { chapter: 'Готово', title: 'Вот и всё!',
    html: 'Порядок работы: <b>пользователи → план → документы → сообщения</b>. Тур можно запустить снова в настройках (шестерёнка внизу слева) — <b>«Как пользоваться»</b>.',
    target: sel('profile-menu'), final: true,
    buttons: [{ act: 'again', label: '↺ Ещё раз' }, { act: 'cabinet', label: 'Тур по кабинету →', primary: true }] },
];

const ADMIN: Scenario = {
  id: 'admin',
  offer: 'Покажем за пару минут, как устроена админка: сайт сам пройдёт по всем разделам в порядке работы.',
  steps: ADMIN_STEPS,
  snapshot: () => ({ path: here(), scrollY: window.scrollY, plans: hooks.plans?.snapshot() }),
  restore: (snap) => {
    const s = snap as { path: string; scrollY: number; plans: unknown } | null;
    hooks.users?.closeDemo();
    hooks.users?.setStaffing(false);
    if (!s) return;
    if (s.plans !== undefined) {
      if (hooks.plans) hooks.plans.restore(s.plans); else pendingPlans = s.plans;
    }
    if (here() !== s.path) navigate(s.path);
    setTimeout(() => window.scrollTo(0, s.scrollY), 200);
  },
  actions: { cabinet: () => { stop(true).then(() => { navigate('/'); setTimeout(() => startTour(), 500); }); } },
};

// ---------------------------------------------------------------- кабинет
const narrow = () => window.innerWidth <= 760;
const cab = (tab: string) => (tab === 'chat' ? '/' : `/?tab=${tab}`);
const askView = async (k: Kit, v: 'dialog' | 'questions') => {
  await go(k, cab('ask'));
  await k.waitFor(() => (hooks.cabinet ? document.body : null), 2000);
  hooks.cabinet?.setAskView(v);
  await k.sleep(120);
};

const CABINET_MOBILE = (): Step[] => [
  { chapter: 'Знакомство', title: 'Ваш помощник по адаптации',
    text: 'Здесь приходят сообщения плана адаптации: что сделать до выхода, в первый день и дальше. А ещё можно задать вопрос — ассистент ответит по документам компании.',
    target: sel('tabbar'), enter: (k) => go(k, cab('chat')) },
  { chapter: 'Сообщения', title: 'Как идёт адаптация',
    text: '«День 3 из 30» — сколько плана позади, текущий этап и что придёт дальше.',
    target: sel('progress'), enter: (k) => go(k, cab('chat')) },
  { chapter: 'Сообщения', title: 'Чат на сегодня',
    text: 'Сообщения за сегодня: старые — сверху, новые — снизу. Чек-листы отмечаются нажатием, в тестах выбирается ответ. Когда придёт новое, вкладка покажет счётчик.',
    target: sel('tab-chat'), click: true, enter: (k) => go(k, cab('chat')) },
  { chapter: 'Сообщения', title: 'История',
    text: 'Сообщения прошлых дней, по датам — от старых к новым.',
    target: sel('tab-history'), click: true, enter: (k) => go(k, cab('history')) },
  { chapter: 'Вопросы', title: 'Задать вопрос',
    text: 'Не нашли ответ в сообщениях? Спросите ассистента — про пропуск, спецодежду, график, зарплату.',
    target: sel('tab-ask'), click: true, enter: (k) => askView(k, 'dialog') },
  { chapter: 'Вопросы', title: 'Напишите вопрос своими словами',
    text: 'Например, так. Ответ придёт сразу, если он есть в документах компании.',
    target: '#nm-question', enter: (k) => askView(k, 'dialog'), act: (k) => k.type('#nm-question', 'Где получить спецодежду?', 55) },
  { chapter: 'Вопросы', title: 'Отправить',
    text: 'Если в документах ответа нет или вопрос срочный (травма, пожар), его увидит специалист — ответ придёт в «Мои вопросы». (Сейчас ничего не отправляем.)',
    target: `${sel('composer')} button`, click: true, enter: (k) => askView(k, 'dialog'), leave: (k) => k.clearTyped() },
  { chapter: 'Вопросы', title: 'Мои вопросы',
    text: 'Вопросы, переданные специалисту, и ответы на них.',
    target: '.nm-seg button:last-child', click: true, enter: (k) => askView(k, 'dialog'), act: (k) => askView(k, 'questions') },
  { chapter: 'Настройки', title: 'Настройки',
    text: 'Профиль, план и наставник, тёмная тема, уведомления и выход.',
    target: sel('tab-settings'), click: true, enter: (k) => go(k, cab('settings')) },
  { chapter: 'Настройки', title: 'Заболели? «Я на больничном»',
    text: 'План встанет на паузу, а наставник получит уведомление. Выключите после выхода — план продолжится с того места, где вы остановились.',
    target: sel('sick'), enter: (k) => go(k, cab('settings')) },
  { chapter: 'Готово', title: 'Готово!',
    html: 'Сообщения — во вкладке <b>«Чат»</b>, вопросы — во вкладке <b>«Вопрос»</b>. Тур можно повторить в <b>«Настройках»</b>.',
    target: sel('tab-settings'), final: true, buttons: [{ act: 'again', label: '↺ Ещё раз' }] },
];

const CABINET_DESKTOP = (): Step[] => [
  { chapter: 'Знакомство', title: 'Ваш помощник по адаптации',
    text: 'Здесь приходят сообщения плана адаптации: что сделать до выхода, в первый день и дальше. А справа — ассистент, который отвечает по документам компании.',
    target: sel('nav-cabinet'), enter: (k) => go(k, '/') },
  { chapter: 'Сообщения', title: 'Как идёт адаптация',
    text: '«День 3 из 30» — сколько плана позади, по какому плану вы идёте и с какой даты.',
    target: sel('progress'), enter: (k) => go(k, '/') },
  { chapter: 'Сообщения', title: 'Сегодня',
    text: 'Сообщения за сегодня: старые — сверху, новые — снизу. Чек-листы отмечаются нажатием, в тестах выбирается ответ — всё сохраняется сразу.',
    target: () => document.querySelector(sel('today')) || document.querySelector('.nm-cabinet .nm-section'), enter: (k) => go(k, '/') },
  { chapter: 'Сообщения', title: 'Дальше по плану',
    text: 'Что и когда придёт в ближайшие дни. Прошлые сообщения — в «Истории сообщений» ниже.',
    target: sel('upnext'), enter: (k) => go(k, '/') },
  { chapter: 'Вопросы', title: 'Ассистент',
    text: 'Спросите про пропуск, спецодежду, график, зарплату — ответ придёт сразу, если он есть в документах.',
    target: sel('assistant'), enter: (k) => go(k, '/') },
  { chapter: 'Вопросы', title: 'Напишите вопрос своими словами',
    text: 'Например, так. Под ответом будет видно, из какого документа он взят.',
    target: '#nm-question', enter: (k) => go(k, '/'), act: (k) => k.type('#nm-question', 'Где получить спецодежду?', 55) },
  { chapter: 'Вопросы', title: 'Отправить',
    text: 'Если ответа в документах нет или вопрос срочный (травма, пожар), его увидит специалист. (Сейчас ничего не отправляем.)',
    target: `${sel('composer')} button`, click: true, enter: (k) => go(k, '/'), leave: (k) => k.clearTyped() },
  { chapter: 'Вопросы', title: 'Мои вопросы специалисту',
    text: 'Вопросы, переданные специалисту, и ответы на них — появятся здесь.',
    target: sel('my-questions'), enter: (k) => go(k, '/') },
  { chapter: 'Настройки', title: 'Заболели? «Я на больничном»',
    text: 'План встанет на паузу, а наставник получит уведомление. Выключите после выхода — план продолжится с того места, где вы остановились.',
    target: sel('sick'), enter: (k) => go(k, '/') },
  { chapter: 'Готово', title: 'Готово!',
    html: 'Тема оформления и этот тур (<b>«Как пользоваться»</b>) — в настройках: шестерёнка внизу слева.',
    target: sel('profile-menu'), final: true, buttons: [{ act: 'again', label: '↺ Ещё раз' }] },
];

const cabinetScenario = (): Scenario => ({
  id: 'cabinet',
  offer: 'Покажем за минуту, где сообщения по адаптации, как задать вопрос и что делать, если заболели.',
  steps: narrow() ? CABINET_MOBILE : CABINET_DESKTOP,
  snapshot: () => ({ path: here(), scrollY: window.scrollY }),
  restore: (snap) => {
    const s = snap as { path: string; scrollY: number } | null;
    hooks.cabinet?.setAskView('dialog');
    if (!s) return;
    if (here() !== s.path) navigate(s.path);
    setTimeout(() => window.scrollTo(0, s.scrollY), 200);
  },
});

function scenarioFor(path: string): Scenario {
  return path === '/' ? cabinetScenario() : ADMIN;
}

/** Запуск тура для текущей страницы (кнопка «Как пользоваться»). */
export function startTour() {
  start(scenarioFor(location.pathname));
}

/** Подключает тур к роутеру: ?tour=1 в адресе — старт, первый визит — предложение. */
export function TourHost() {
  const nav = useNavigate();
  const { pathname } = useLocation();
  useEffect(() => { navigate = (to) => nav(to); }, [nav]);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if (params.get('tour') === '1') {
      params.delete('tour');
      nav({ pathname, search: params.toString() ? `?${params}` : '' }, { replace: true });
      const t = window.setTimeout(() => startTour(), 700);
      return () => window.clearTimeout(t);
    }
    const t = window.setTimeout(() => maybeOffer(scenarioFor(pathname)), 1500);
    return () => window.clearTimeout(t);
  }, [pathname, nav]);
  return null;
}
