// Оболочка сайта (PRINCIPLES.md → AppShell): сайдбар 250px — группы «Сотрудник» и
// «Администратор», внизу карточка профиля с меню (тема, пароль, служебные страницы, выход).
import { useCallback, useState, type ReactNode } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import {
  CalendarRange, ClipboardList, Database, GraduationCap, House, KeyRound, LogOut, MessageCircleQuestion, MessagesSquare, Moon, Settings, Smartphone, Sun, Users,
} from 'lucide-react';
import { initials } from '@shared/format';
import { ROLE } from '@shared/status';
import { useMe } from '../lib/me';
import { useInbox } from '../lib/inbox';
import { useThemeMode } from '../lib/theme';
import { api } from '../lib/api';
import { usePolling } from '../lib/poll';
import { startTour } from '../tour';
import { Avatar, Button, Count, Logo } from '../ui';
import { Menu } from '../ui/Menu';
import { PasswordDialog } from './PasswordDialog';

export const ADMIN_NAV = [
  { to: '/admin/users', label: 'Пользователи', icon: Users, tour: 'nav-users' },
  { to: '/admin/plans', label: 'Планы', icon: CalendarRange, tour: 'nav-plans' },
  { to: '/admin/documents', label: 'Документы', icon: Database, tour: 'nav-documents' },
  { to: '/admin/messages', label: 'Сообщения', icon: MessagesSquare, tour: 'nav-messages' },
  { to: '/admin/questions', label: 'Вопросы', icon: MessageCircleQuestion, tour: 'nav-questions' },
];

export async function logout() {
  try { await api.logout(); } catch { /* сессия уже могла истечь */ }
  location.assign('/login');
}

function useOpenQuestions(enabled: boolean) {
  const [n, setN] = useState(0);
  const load = useCallback(() => {
    api.get<{ open_count?: number }>('/questions?status=open').then((d) => setN(d.open_count || 0)).catch(() => {});
  }, []);
  usePolling(load, enabled ? 60000 : 0, [enabled]);
  return n;
}

export function AppShell({ children }: { children: ReactNode }) {
  const { me, isAdmin } = useMe();
  const { unread } = useInbox();
  const { dark, toggle } = useThemeMode();
  const openQuestions = useOpenQuestions(isAdmin);
  const [pwOpen, setPwOpen] = useState(false);
  const navigate = useNavigate();
  const role = me ? ROLE[me.role]?.label.toLowerCase() : '';
  return (
    <div className="nm-shell">
      <aside className="nm-sidebar" aria-label="Навигация">
        <a className="nm-brand" href="/" onClick={(e) => { e.preventDefault(); navigate('/'); }}><Logo /><span>НейроМастер</span></a>
        <nav className="nm-nav" aria-label="Разделы">
          <div className="nm-nav-group">
            <div className="nm-nav-label">Сотрудник</div>
            <NavLink to="/" end className="nm-nav-item" data-tour="nav-cabinet">
              <span><House aria-hidden />Мой кабинет</span><Count n={unread} />
            </NavLink>
            <NavLink to="/app" className="nm-nav-item" data-tour="nav-app">
              <span><Smartphone aria-hidden />Приложение</span>
            </NavLink>
          </div>
          {isAdmin && (
            <div className="nm-nav-group" style={{ marginTop: 18 }} data-tour="admin-nav">
              <div className="nm-nav-label">Администратор</div>
              {ADMIN_NAV.map((it) => (
                <NavLink key={it.to} to={it.to} className="nm-nav-item" data-tour={it.tour}>
                  <span><it.icon aria-hidden />{it.label}</span>
                  {it.to === '/admin/questions' && <Count n={openQuestions} />}
                </NavLink>
              ))}
            </div>
          )}
        </nav>
        <div className="nm-sidebar-foot">
          <div className="nm-profile" data-tour="profile">
            <Avatar text={initials(me?.full_name || me?.username)} />
            <span className="nm-grow nm-profile-text">
              <span className="nm-profile-name">{me?.full_name || me?.username || '…'}</span>
              <span className="nm-profile-role">{role}</span>
            </span>
            <Menu side="top" anchor=".nm-profile" label="Настройки" items={[
              { label: 'Как пользоваться', icon: GraduationCap, onClick: () => startTour() },
              { label: dark ? 'Светлая тема' : 'Тёмная тема', icon: dark ? Sun : Moon, onClick: toggle },
              { label: 'Сменить пароль', icon: KeyRound, onClick: () => setPwOpen(true), hidden: !isAdmin },
              { section: 'Служебное', hidden: !isAdmin },
              { label: 'Журнал действий', icon: ClipboardList, href: '/logs', hidden: !isAdmin },
            ]} trigger={(p) => <Button variant="ghost" iconOnly size="sm" icon={Settings} aria-label="Настройки" title="Настройки" data-tour="profile-menu" {...p} />} />
            <Button variant="ghost" iconOnly size="sm" icon={LogOut} aria-label="Выйти" title="Выйти" onClick={logout} />
          </div>
        </div>
      </aside>
      <main className="nm-main" id="main">{children}</main>
      <PasswordDialog open={pwOpen} onClose={() => setPwOpen(false)} />
    </div>
  );
}
