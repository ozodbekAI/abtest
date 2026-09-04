import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { BarChart3, Check, ChevronRight, Eye, EyeOff, FlaskConical, KeyRound, LockKeyhole, LogOut, Menu, Settings, ShieldCheck, Sparkles, UserRound, X } from 'lucide-react';
import { NavLink, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';

export function AppShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileBusy, setProfileBusy] = useState(false);
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [profile, setProfile] = useState({ first_name: '', last_name: '' });
  const [passwords, setPasswords] = useState({ current: '', next: '', confirm: '' });
  const [visiblePasswords, setVisiblePasswords] = useState({ current: false, next: false, confirm: false });
  const { user, logout, updateProfile } = useAuth();
  const navigate = useNavigate();
  const initials = `${user?.first_name?.[0] || ''}${user?.last_name?.[0] || ''}`.toUpperCase();

  useEffect(() => {
    if (user) setProfile({ first_name: user.first_name, last_name: user.last_name });
  }, [user]);

  const signOut = async () => { await logout(); navigate('/login'); };

  const saveProfile = async (event: FormEvent) => {
    event.preventDefault();
    setProfileBusy(true);
    try {
      await updateProfile(profile.first_name, profile.last_name);
      toast.success('Профиль обновлён');
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось обновить профиль');
    } finally {
      setProfileBusy(false);
    }
  };

  const savePassword = async (event: FormEvent) => {
    event.preventDefault();
    if (passwords.next !== passwords.confirm) {
      toast.error('Новые пароли не совпадают');
      return;
    }
    setPasswordBusy(true);
    try {
      await api.changePassword(passwords.current, passwords.next);
      setPasswords({ current: '', next: '', confirm: '' });
      toast.success('Пароль обновлён. Войдите снова.');
      await logout();
      navigate('/login', { replace: true });
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось изменить пароль');
    } finally {
      setPasswordBusy(false);
    }
  };

  const closeProfile = () => setProfileOpen(false);

  return <div className="app-shell">
    <aside className={`sidebar ${open ? 'sidebar-open' : ''}`}>
      <div className="brand"><span className="brand-mark">W</span> WB Optimizer</div>
      <div className="workspace-badge"><span className="status-pulse" /> Личное пространство</div>
      <nav className="side-nav">
        <NavLink to="/dashboard" onClick={() => setOpen(false)}><BarChart3 size={18} /> Обзор</NavLink>
        <NavLink to="/ab-tests" onClick={() => setOpen(false)}><FlaskConical size={18} /> A/B-тесты</NavLink>
        <NavLink to="/settings" onClick={() => setOpen(false)}><Settings size={18} /> Настройки магазинов</NavLink>
        {user?.is_admin && <NavLink to="/admin" onClick={() => setOpen(false)}><ShieldCheck size={18} /> Админ-панель</NavLink>}
      </nav>
      <div className="sidebar-bottom">
        <div className="side-tip"><Sparkles size={16} /><div><strong>Следующий шаг</strong><span>Подключите токен WB</span></div></div>
        <button className="side-logout" onClick={() => void signOut()}><LogOut size={17} /> Выйти</button>
      </div>
    </aside>
    <button className="mobile-menu" onClick={() => setOpen((value) => !value)} aria-label="Открыть меню"><Menu size={20} /></button>
    {open && <button className="sidebar-backdrop" onClick={() => setOpen(false)} aria-label="Закрыть меню" />}
    <main className="app-content">
      <header className="topbar"><div className="breadcrumb">Рабочее пространство <ChevronRight size={14} /> <span>WB Optimizer</span></div><button className="user-chip" onClick={() => setProfileOpen(true)} aria-label="Открыть профиль"><div className="avatar">{initials || <UserRound size={15} />}</div><span>{user?.first_name || user?.email}</span></button></header>
      {children}
    </main>
    {profileOpen && <div className="profile-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeProfile(); }}><section className="profile-modal" role="dialog" aria-modal="true" aria-labelledby="profile-modal-title"><button type="button" className="icon-button profile-modal-close" onClick={closeProfile} aria-label="Закрыть"><X size={17} /></button><div className="profile-modal-avatar">{initials || <UserRound size={20} />}</div><span className="modal-kicker">ПРОФИЛЬ</span><h2 id="profile-modal-title">Настройки профиля</h2><p>Управляйте личными данными и безопасностью аккаунта в одном месте.</p><section className="profile-modal-section"><div className="profile-section-heading"><div><span className="modal-kicker">ЛИЧНЫЕ ДАННЫЕ</span><h3>Ваш профиль</h3></div><UserRound size={19} /></div><form onSubmit={saveProfile} className="profile-modal-form"><div className="form-grid"><label>Имя<input value={profile.first_name} onChange={(event) => setProfile((current) => ({ ...current, first_name: event.target.value }))} required minLength={2} maxLength={80} autoFocus /></label><label>Фамилия<input value={profile.last_name} onChange={(event) => setProfile((current) => ({ ...current, last_name: event.target.value }))} required minLength={2} maxLength={80} /></label></div><label>Электронная почта<input value={user?.email || ''} readOnly className="readonly-input" /></label><button className="primary-button" disabled={profileBusy}>{profileBusy ? 'Сохранение…' : 'Сохранить данные'}<Check size={17} /></button></form></section><div className="profile-modal-divider" /><section className="profile-modal-section"><div className="profile-section-heading"><div><span className="modal-kicker">БЕЗОПАСНОСТЬ</span><h3>Смена пароля</h3></div><ShieldCheck size={19} /></div><p className="profile-section-description">Новый пароль должен содержать не менее 8 символов.</p><form onSubmit={savePassword} className="profile-password-form"><PasswordField label="Текущий пароль" value={passwords.current} visible={visiblePasswords.current} onChange={(value) => setPasswords((old) => ({ ...old, current: value }))} onToggle={() => setVisiblePasswords((old) => ({ ...old, current: !old.current }))} /><PasswordField label="Новый пароль" value={passwords.next} visible={visiblePasswords.next} onChange={(value) => setPasswords((old) => ({ ...old, next: value }))} onToggle={() => setVisiblePasswords((old) => ({ ...old, next: !old.next }))} /><PasswordField label="Подтвердите новый пароль" value={passwords.confirm} visible={visiblePasswords.confirm} onChange={(value) => setPasswords((old) => ({ ...old, confirm: value }))} onToggle={() => setVisiblePasswords((old) => ({ ...old, confirm: !old.confirm }))} /><button className="primary-button" disabled={passwordBusy}>{passwordBusy ? 'Сохранение…' : 'Изменить пароль'}<LockKeyhole size={16} /></button></form></section></section></div>}
  </div>;
}

function PasswordField({ label, value, visible, onChange, onToggle }: { label: string; value: string; visible: boolean; onChange: (value: string) => void; onToggle: () => void }) {
  return <label className="password-field">{label}<div className="input-with-action"><input type={visible ? 'text' : 'password'} value={value} onChange={(event) => onChange(event.target.value)} required minLength={8} autoComplete="current-password" /><button type="button" onClick={onToggle} aria-label={visible ? `Скрыть: ${label.toLowerCase()}` : `Показать: ${label.toLowerCase()}`}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label>;
}
