import { useEffect, useState, type FormEvent } from 'react';
import { Activity, ArrowUpRight, Check, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert, Eye, EyeOff, FlaskConical, LockKeyhole, Mail, RefreshCw, Search, Settings2, ShieldCheck, Store, Trash2, UserPlus, Users, X } from 'lucide-react';
import { Link, Navigate, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { AppShell } from '../components/AppShell';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import type { AdminDashboard, AdminSettings, AdminStoreDetail, AdminStoreItem, AdminTestItem, AdminUserDetail, AdminUserItem } from '../types';

type AdminTab = 'overview' | 'users' | 'stores' | 'settings';

function isAdminTab(value: string | null): value is AdminTab {
  return value === 'overview' || value === 'users' || value === 'stores' || value === 'settings';
}

const emptyCreateForm = { email: '', password: '', confirm_password: '', first_name: '', last_name: '', is_verified: true, is_active: true, is_admin: false };

function number(value: number) { return new Intl.NumberFormat('ru-RU').format(Math.round(value || 0)); }
function rub(value: number) { return `${new Intl.NumberFormat('ru-RU').format(Math.round(value || 0))} ₽`; }
function dateTime(value?: string | null) { return value ? new Date(value).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—'; }
function statusLabel(value: string) { return ({ running: 'Активен', finished: 'Завершён', failed: 'Ошибка', stopped: 'Остановлен', draft: 'Черновик' } as Record<string, string>)[value] || value; }

export function AdminPage() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialTab = searchParams.get('tab');
  const [tab, setTab] = useState<AdminTab>(() => isAdminTab(initialTab) ? initialTab : 'overview');
  const [dashboard, setDashboard] = useState<AdminDashboard | null>(null);
  const [users, setUsers] = useState<AdminUserItem[]>([]);
  const [usersTotal, setUsersTotal] = useState(0);
  const [stores, setStores] = useState<AdminStoreItem[]>([]);
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [search, setSearch] = useState('');
  const [userPage, setUserPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState(emptyCreateForm);
  const [createBusy, setCreateBusy] = useState(false);
  const [selectedUser, setSelectedUser] = useState<AdminUserDetail | null>(null);
  const [selectedStore, setSelectedStore] = useState<AdminStoreDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [settingsBusy, setSettingsBusy] = useState(false);

  const loadData = async (silent = false) => {
    if (!user?.is_admin) return;
    silent ? setRefreshing(true) : setLoading(true);
    try {
      const [dashboardResult, userResult, storesResult, settingsResult] = await Promise.all([
        api.getAdminDashboard(),
        api.getAdminUsers(search, userPage),
        api.getAdminStores(),
        api.getAdminSettings(),
      ]);
      setDashboard(dashboardResult);
      setUsers(userResult.items);
      setUsersTotal(userResult.total);
      setStores(storesResult);
      setSettings(settingsResult);
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось загрузить данные админ-панели');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => { void loadData(); }, [user?.is_admin, userPage]);

  useEffect(() => {
    const requested = searchParams.get('tab');
    if (isAdminTab(requested) && requested !== tab) setTab(requested);
  }, [searchParams, tab]);

  if (!user?.is_admin) return <Navigate to="/dashboard" replace />;

  const searchUsers = async (event: FormEvent) => {
    event.preventDefault();
    setUserPage(1);
    try {
      const result = await api.getAdminUsers(search, 1);
      setUsers(result.items);
      setUsersTotal(result.total);
    } catch (error: any) { toast.error(error?.message || 'Не удалось найти пользователей'); }
  };

  const selectTab = (next: AdminTab) => {
    setTab(next);
    setSearchParams(next === 'overview' ? {} : { tab: next }, { replace: true });
  };

  const openUser = async (item: AdminUserItem) => {
    if (item.id === user?.id) {
      toast.info('Собственную учётную запись нельзя изменять через админ-панель');
      return;
    }
    setDetailLoading(true);
    try { setSelectedUser(await api.getAdminUser(item.id)); }
    catch (error: any) { toast.error(error?.message || 'Не удалось открыть пользователя'); }
    finally { setDetailLoading(false); }
  };

  const openStore = async (item: AdminStoreItem) => {
    setDetailLoading(true);
    try { setSelectedStore(await api.getAdminStore(item.id)); }
    catch (error: any) { toast.error(error?.message || 'Не удалось открыть магазин'); }
    finally { setDetailLoading(false); }
  };

  const createUser = async (event: FormEvent) => {
    event.preventDefault();
    setCreateBusy(true);
    try {
      await api.createAdminUser(createForm);
      toast.success('Пользователь создан');
      setCreateForm(emptyCreateForm);
      setShowCreate(false);
      await loadData(true);
    } catch (error: any) { toast.error(error?.message || 'Не удалось создать пользователя'); }
    finally { setCreateBusy(false); }
  };

  const updateRegistration = async (enabled: boolean) => {
    setSettingsBusy(true);
    try {
      setSettings(await api.updateAdminSettings(enabled));
      toast.success(enabled ? 'Регистрация включена' : 'Регистрация отключена');
    } catch (error: any) { toast.error(error?.message || 'Не удалось сохранить настройку'); }
    finally { setSettingsBusy(false); }
  };

  const tabItems: Array<[AdminTab, string, typeof Activity]> = [
    ['overview', 'Обзор', Activity],
    ['users', 'Пользователи', Users],
    ['stores', 'Магазины и тесты', Store],
    ['settings', 'Глобальные настройки', Settings2],
  ];

  return <AppShell>
    <div className="admin-page page-wrap">
      <div className="admin-heading page-heading"><div><div className="eyebrow eyebrow-dark"><ShieldCheck size={14} /> Центр управления</div><h1>Админ-панель</h1><p>Пользователи, магазины, A/B-тесты и настройки платформы в одном месте.</p></div><button className="outline-button" onClick={() => void loadData(true)} disabled={refreshing}><RefreshCw size={16} className={refreshing ? 'spin' : ''} /> Обновить</button></div>
      <nav className="admin-tabs" aria-label="Разделы админ-панели">{tabItems.map(([value, label, Icon]) => <button type="button" key={value} className={tab === value ? 'active' : ''} onClick={() => selectTab(value)}><Icon size={16} />{label}</button>)}</nav>
      {loading ? <div className="admin-loading"><div className="loader-dot" /><span>Загружаем данные панели…</span></div> : <>
        {tab === 'overview' && dashboard && <AdminOverview data={dashboard} />}
        {tab === 'users' && <AdminUsers users={users} currentUserId={user?.id} total={usersTotal} page={userPage} search={search} setSearch={setSearch} onSearch={searchUsers} onPageChange={setUserPage} onCreate={() => setShowCreate(true)} onOpen={openUser} />}
        {tab === 'stores' && <AdminStores stores={stores} onOpen={openStore} />}
        {tab === 'settings' && <AdminSettingsPanel settings={settings} busy={settingsBusy} onRegistrationChange={updateRegistration} />}
      </>}
    </div>
    {showCreate && <CreateUserModal form={createForm} setForm={setCreateForm} busy={createBusy} onSubmit={createUser} onClose={() => setShowCreate(false)} />}
    {selectedUser && <UserDetailModal detail={selectedUser} onClose={() => setSelectedUser(null)} onDeleted={async () => { setSelectedUser(null); await loadData(true); }} onReload={async () => { setSelectedUser(await api.getAdminUser(selectedUser.user.id)); await loadData(true); }} />}
    {selectedStore && <StoreDetailModal detail={selectedStore} onClose={() => setSelectedStore(null)} />}
    {detailLoading && <div className="admin-detail-loader"><div className="loader-dot" /></div>}
  </AppShell>;
}

function AdminOverview({ data }: { data: AdminDashboard }) {
  return <div className="admin-overview"><section className="admin-kpi-grid"><AdminKpi icon={<Users />} label="Пользователи" value={number(data.users_count)} hint={`${number(data.active_users_count)} активных`} /><AdminKpi icon={<Store />} label="Магазины" value={number(data.stores_count)} hint="Подключено к платформе" /><AdminKpi icon={<FlaskConical />} label="A/B-тесты" value={number(data.tests_count)} hint={`${number(data.running_tests_count)} активных`} /><AdminKpi icon={<Eye />} label="Показы" value={number(data.views)} hint={`${number(data.clicks)} кликов`} /><AdminKpi icon={<ShoppingIcon />} label="Расход" value={rub(data.spend_rub)} hint={`${number(data.orders)} заказов`} /></section><section className="admin-insight-grid"><section className="admin-chart-card"><div className="admin-card-heading"><div><span className="settings-kicker">АКТИВНОСТЬ ПЛАТФОРМЫ</span><h2>Регистрации за 30 дней</h2><p>Новые пользователи по дате создания аккаунта.</p></div><span className="admin-chart-total">{number(data.users_count)} всего</span></div><AdminRegistrationChart points={data.registrations} /></section><section className="admin-health-card"><span className="settings-kicker">СОСТОЯНИЕ СИСТЕМЫ</span><h2>Контроль платформы</h2><div className="admin-health-list"><HealthRow label="Подтверждённые аккаунты" value={number(data.verified_users_count)} total={data.users_count} /><HealthRow label="Завершённые тесты" value={number(data.finished_tests_count)} total={data.tests_count} /><HealthRow label="Ошибки и остановки" value={number(data.failed_tests_count)} total={data.tests_count} warning /></div><Link to="/admin?tab=users" className="admin-health-link">Управлять пользователями <ArrowUpRight size={14} /></Link></section></section></div>;
}

function AdminKpi({ icon, label, value, hint }: { icon: React.ReactNode; label: string; value: string; hint: string }) { return <div className="admin-kpi"><div className="admin-kpi-icon">{icon}</div><span>{label}</span><strong>{value}</strong><small>{hint}</small></div>; }
function ShoppingIcon() { return <span className="admin-shopping-icon">₽</span>; }
function HealthRow({ label, value, total, warning = false }: { label: string; value: string; total: number; warning?: boolean }) { const numeric = Number(value.replace(/\D/g, '')) || 0; const width = total ? Math.min(100, (numeric / total) * 100) : 0; return <div className="admin-health-row"><div><span>{label}</span><strong className={warning ? 'warning-value' : ''}>{value}</strong></div><div className="admin-health-track"><i className={warning ? 'warning' : ''} style={{ width: `${width}%` }} /></div></div>; }

function AdminRegistrationChart({ points }: { points: AdminDashboard['registrations'] }) {
  const width = 760; const height = 240; const left = 12; const right = 12; const top = 18; const bottom = 30; const plotWidth = width - left - right; const plotHeight = height - top - bottom; const max = Math.max(1, ...points.map((point) => point.count)); const x = (index: number) => points.length === 1 ? width / 2 : left + (index / (points.length - 1)) * plotWidth; const y = (value: number) => top + plotHeight - (value / max) * plotHeight; const line = points.map((point, index) => `${x(index)},${y(point.count)}`).join(' ');
  return <div className="admin-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Регистрации пользователей за 30 дней"><defs><linearGradient id="adminRegistrationsFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="#4d7ff0" stopOpacity=".2" /><stop offset="1" stopColor="#4d7ff0" stopOpacity="0" /></linearGradient></defs>{[0, .25, .5, .75, 1].map((ratio) => <line key={ratio} x1={left} x2={width - right} y1={top + plotHeight * ratio} y2={top + plotHeight * ratio} className="admin-chart-grid-line" />)}{points.length > 0 && <><polygon points={`${left},${top + plotHeight} ${line} ${width - right},${top + plotHeight}`} className="admin-chart-area" /><polyline points={line} className="admin-chart-line" fill="none" />{points.map((point, index) => <circle key={point.date} cx={x(index)} cy={y(point.count)} r="3.5" className="admin-chart-point" />)}</>}</svg><div className="admin-chart-axis">{points.filter((_, index) => index === 0 || index === points.length - 1 || index === Math.floor(points.length / 2)).map((point) => <span key={point.date}>{new Date(`${point.date}T00:00:00`).toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' }).replace('.', '')}</span>)}</div></div>;
}

function AdminUsers({ users, currentUserId, total, page, search, setSearch, onSearch, onPageChange, onCreate, onOpen }: { users: AdminUserItem[]; currentUserId?: number; total: number; page: number; search: string; setSearch: (value: string) => void; onSearch: (event: FormEvent) => void; onPageChange: (page: number) => void; onCreate: () => void; onOpen: (user: AdminUserItem) => void }) {
  const pages = Math.max(1, Math.ceil(total / 20));
  return <section className="admin-table-card"><div className="admin-section-toolbar"><div><span className="settings-kicker">АККАУНТЫ</span><h2>Пользователи <small>{number(total)}</small></h2><p>Управляйте доступом, ролями и данными аккаунтов.</p></div><button className="primary-button" onClick={onCreate}><UserPlus size={16} /> Добавить пользователя</button></div><form className="admin-search" onSubmit={onSearch}><Search size={17} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по email, имени или фамилии" /><button type="submit">Найти</button></form><div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>Пользователь</th><th>Статус</th><th>Роль</th><th>Магазины</th><th>A/B-тесты</th><th>Дата регистрации</th><th /></tr></thead><tbody>{users.length ? users.map((item) => { const isSelf = item.id === currentUserId; return <tr key={item.id}><td><button type="button" className="admin-user-cell" onClick={() => !isSelf && onOpen(item)} disabled={isSelf}><span className="admin-user-avatar">{`${item.first_name[0] || ''}${item.last_name[0] || ''}`.toUpperCase() || <Users size={14} />}</span><span><strong>{item.first_name} {item.last_name}</strong><small>{item.email}{isSelf ? ' · Это вы' : ''}</small></span></button></td><td><span className={`admin-status ${item.is_active ? 'active' : 'blocked'}`}><i />{item.is_active ? 'Активен' : 'Отключён'}</span>{!item.is_verified && <small className="admin-unverified">Не подтверждён</small>}</td><td>{item.is_admin ? <span className="admin-role admin-role-admin"><ShieldCheck size={13} /> Админ</span> : <span className="admin-role">Пользователь</span>}</td><td>{number(item.stores_count)}</td><td>{number(item.tests_count)}</td><td>{new Date(item.created_at).toLocaleDateString('ru-RU')}</td><td><button type="button" className="table-action" onClick={() => onOpen(item)} disabled={isSelf} aria-label={isSelf ? "Текущий администратор" : "Открыть пользователя"}><ChevronRight size={17} /></button></td></tr>; }) : <tr><td colSpan={7}><div className="admin-empty"><Users size={22} /><strong>Пользователи не найдены</strong><span>Измените поисковый запрос и попробуйте снова.</span></div></td></tr>}</tbody></table></div><div className="admin-pagination"><span>Страница {page} из {pages}</span><div><button className="table-action" disabled={page <= 1} onClick={() => onPageChange(page - 1)}><ChevronLeft size={16} /></button><button className="table-action" disabled={page >= pages} onClick={() => onPageChange(page + 1)}><ChevronRight size={16} /></button></div></div></section>;
}

function AdminStores({ stores, onOpen }: { stores: AdminStoreItem[]; onOpen: (store: AdminStoreItem) => void }) {
  return <section className="admin-table-card"><div className="admin-section-toolbar"><div><span className="settings-kicker">ПОДКЛЮЧЕНИЯ</span><h2>Магазины Wildberries <small>{number(stores.length)}</small></h2><p>Состояние подключений и A/B-тесты владельцев.</p></div><Link to="/settings" className="outline-button"><Store size={16} /> Настройки магазинов</Link></div><div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>Магазин</th><th>Владелец</th><th>Доступ</th><th>Тесты</th><th>Токен</th><th>Проверка</th><th /></tr></thead><tbody>{stores.length ? stores.map((item) => <tr key={item.id}><td><div className="admin-store-cell"><span className="admin-store-icon"><Store size={15} /></span><strong>{item.store_name}</strong></div></td><td><span className="admin-owner"><strong>{item.owner_name}</strong><small>{item.owner_email}</small></span></td><td><span className={`admin-status ${item.ready_for_ab_tests ? 'active' : 'blocked'}`}><i />{item.ready_for_ab_tests ? 'Готов' : 'Проверить'}</span></td><td>{number(item.tests_count)}</td><td>••••{item.token_last4}</td><td>{dateTime(item.last_validated_at)}</td><td><button className="table-action" onClick={() => onOpen(item)} aria-label="Открыть магазин"><ChevronRight size={17} /></button></td></tr>) : <tr><td colSpan={7}><div className="admin-empty"><Store size={22} /><strong>Магазинов пока нет</strong><span>Подключения появятся после добавления токена.</span></div></td></tr>}</tbody></table></div></section>;
}

function AdminSettingsPanel({ settings, busy, onRegistrationChange }: { settings: AdminSettings | null; busy: boolean; onRegistrationChange: (enabled: boolean) => void }) {
  const enabled = settings?.registration_enabled ?? true;
  return <section className="admin-settings-layout"><div className="admin-settings-main"><span className="settings-kicker">ПЛАТФОРМА</span><h2>Глобальные настройки</h2><p>Изменения применяются сразу ко всем пользователям и новым сессиям.</p><div className="admin-setting-row"><div className="admin-setting-icon"><Users size={19} /></div><div><strong>Регистрация новых пользователей</strong><span>{enabled ? 'Новые пользователи могут создавать аккаунты.' : 'Новые регистрации заблокированы.'}</span></div><button type="button" className={`admin-toggle ${enabled ? 'on' : ''}`} onClick={() => onRegistrationChange(!enabled)} disabled={busy} aria-pressed={enabled}><i /></button></div></div><div className={`admin-warning-card ${enabled ? '' : 'is-disabled'}`}><CircleAlert size={19} /><div><strong>{enabled ? 'Регистрация открыта' : 'Регистрация отключена'}</strong><p>{enabled ? 'Отключите её во время технических работ или закрытого запуска.' : 'Существующие пользователи по-прежнему могут входить в систему.'}</p></div></div></section>;
}

function CreateUserModal({ form, setForm, busy, onSubmit, onClose }: { form: typeof emptyCreateForm; setForm: (form: typeof emptyCreateForm) => void; busy: boolean; onSubmit: (event: FormEvent) => void; onClose: () => void }) {
  const [visible, setVisible] = useState(false);
  return <div className="admin-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section className="admin-modal" role="dialog" aria-modal="true"><button className="icon-button admin-modal-close" onClick={onClose} aria-label="Закрыть"><X size={17} /></button><div className="admin-modal-icon"><UserPlus size={20} /></div><span className="settings-kicker">НОВЫЙ АККАУНТ</span><h2>Добавить пользователя</h2><p>Аккаунт будет создан и сразу появится в списке пользователей.</p><form onSubmit={onSubmit} className="admin-form"><div className="admin-form-two"><label>Имя<input value={form.first_name} onChange={(event) => setForm({ ...form, first_name: event.target.value })} required minLength={2} /></label><label>Фамилия<input value={form.last_name} onChange={(event) => setForm({ ...form, last_name: event.target.value })} placeholder="Необязательно" /></label></div><label>Электронная почта<div className="admin-input-icon"><input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} required /><Mail size={16} /></div></label><label>Пароль<div className="admin-password-input"><input type={visible ? 'text' : 'password'} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} required minLength={8} /><button type="button" onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></div></label><label>Подтверждение пароля<input type={visible ? 'text' : 'password'} value={form.confirm_password} onChange={(event) => setForm({ ...form, confirm_password: event.target.value })} required minLength={8} /></label><div className="admin-checks"><label><input type="checkbox" checked={form.is_verified} onChange={(event) => setForm({ ...form, is_verified: event.target.checked })} /><span>Почта подтверждена</span></label><label><input type="checkbox" checked={form.is_admin} onChange={(event) => setForm({ ...form, is_admin: event.target.checked })} /><span>Права администратора</span></label></div><button className="primary-button" disabled={busy}>{busy ? 'Создание…' : 'Создать пользователя'}<Check size={17} /></button></form></section></div>;
}

function UserDetailModal({ detail, onClose, onReload, onDeleted }: { detail: AdminUserDetail; onClose: () => void; onReload: () => Promise<void>; onDeleted: () => Promise<void> }) {
  const [form, setForm] = useState({ email: detail.user.email, first_name: detail.user.first_name, last_name: detail.user.last_name, is_verified: detail.user.is_verified, is_active: detail.user.is_active, is_admin: detail.user.is_admin });
  const [password, setPassword] = useState({ value: '', confirm: '' });
  const [busy, setBusy] = useState(false);
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [visible, setVisible] = useState(false);
  useEffect(() => { setForm({ email: detail.user.email, first_name: detail.user.first_name, last_name: detail.user.last_name, is_verified: detail.user.is_verified, is_active: detail.user.is_active, is_admin: detail.user.is_admin }); }, [detail.user.email, detail.user.first_name, detail.user.last_name, detail.user.is_verified, detail.user.is_active, detail.user.is_admin]);
  const save = async (event: FormEvent) => { event.preventDefault(); setBusy(true); try { await api.updateAdminUser(detail.user.id, form); toast.success('Данные пользователя обновлены'); await onReload(); } catch (error: any) { toast.error(error?.message || 'Не удалось сохранить пользователя'); } finally { setBusy(false); } };
  const savePassword = async (event: FormEvent) => { event.preventDefault(); if (password.value !== password.confirm) { toast.error('Пароли не совпадают'); return; } setPasswordBusy(true); try { await api.changeAdminUserPassword(detail.user.id, password.value, password.confirm); setPassword({ value: '', confirm: '' }); toast.success('Пароль изменён, сессии завершены'); } catch (error: any) { toast.error(error?.message || 'Не удалось изменить пароль'); } finally { setPasswordBusy(false); } };
  const remove = async () => { if (!window.confirm(`Удалить пользователя ${detail.user.email}? Все его магазины и тесты будут удалены.`)) return; try { await api.deleteAdminUser(detail.user.id); toast.success('Пользователь удалён'); await onDeleted(); } catch (error: any) { toast.error(error?.message || 'Не удалось удалить пользователя'); } };
  return <div className="admin-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section className="admin-modal admin-user-modal" role="dialog" aria-modal="true"><button className="icon-button admin-modal-close" onClick={onClose} aria-label="Закрыть"><X size={17} /></button><div className="admin-user-modal-heading"><span className="admin-user-avatar large">{`${detail.user.first_name[0] || ''}${detail.user.last_name[0] || ''}`.toUpperCase()}</span><div><span className="settings-kicker">ПОЛЬЗОВАТЕЛЬ #{detail.user.id}</span><h2>{detail.user.first_name} {detail.user.last_name}</h2><p>Зарегистрирован: {dateTime(detail.user.created_at)}</p></div></div><form onSubmit={save} className="admin-form"><div className="admin-form-two"><label>Имя<input value={form.first_name} onChange={(event) => setForm({ ...form, first_name: event.target.value })} required minLength={2} /></label><label>Фамилия<input value={form.last_name} onChange={(event) => setForm({ ...form, last_name: event.target.value })} required minLength={2} /></label></div><label>Электронная почта<input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} required /></label><div className="admin-checks"><label><input type="checkbox" checked={form.is_active} onChange={(event) => setForm({ ...form, is_active: event.target.checked })} /><span>Аккаунт активен</span></label><label><input type="checkbox" checked={form.is_verified} onChange={(event) => setForm({ ...form, is_verified: event.target.checked })} /><span>Почта подтверждена</span></label><label><input type="checkbox" checked={form.is_admin} onChange={(event) => setForm({ ...form, is_admin: event.target.checked })} /><span>Администратор</span></label></div><button className="primary-button" disabled={busy}>{busy ? 'Сохранение…' : 'Сохранить данные'}<Check size={16} /></button></form><div className="admin-modal-divider" /><form onSubmit={savePassword} className="admin-form"><div className="admin-subheading"><LockKeyhole size={17} /><div><strong>Сменить пароль</strong><span>Все активные сессии пользователя будут завершены.</span></div></div><label>Новый пароль<div className="admin-password-input"><input type={visible ? 'text' : 'password'} value={password.value} onChange={(event) => setPassword({ ...password, value: event.target.value })} minLength={8} required /><button type="button" onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></div></label><label>Подтверждение пароля<input type={visible ? 'text' : 'password'} value={password.confirm} onChange={(event) => setPassword({ ...password, confirm: event.target.value })} minLength={8} required /></label><button className="outline-button" disabled={passwordBusy}>{passwordBusy ? 'Сохранение…' : 'Изменить пароль'}<LockKeyhole size={16} /></button></form><div className="admin-user-assets"><div><span>Магазины</span><strong>{number(detail.stores.length)}</strong></div><div><span>A/B-тесты</span><strong>{number(detail.tests.length)}</strong></div></div><div className="admin-danger-zone"><button className="danger-button" onClick={() => void remove()}><Trash2 size={15} /> Удалить пользователя</button></div></section></div>;
}

function StoreDetailModal({ detail, onClose }: { detail: AdminStoreDetail; onClose: () => void }) { return <div className="admin-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section className="admin-modal admin-store-modal" role="dialog" aria-modal="true"><button className="icon-button admin-modal-close" onClick={onClose} aria-label="Закрыть"><X size={17} /></button><div className="admin-user-modal-heading"><span className="admin-store-icon large"><Store size={20} /></span><div><span className="settings-kicker">МАГАЗИН #{detail.store.id}</span><h2>{detail.store.store_name}</h2><p>{detail.store.owner_name} · {detail.store.owner_email}</p></div></div><div className="admin-store-meta"><span className={`admin-status ${detail.store.ready_for_ab_tests ? 'active' : 'blocked'}`}><i />{detail.store.ready_for_ab_tests ? 'Готов к A/B-тестам' : 'Требует проверки'}</span><span>Токен ••••{detail.store.token_last4}</span></div><h3 className="admin-modal-list-title">A/B-тесты <small>{number(detail.tests.length)}</small></h3><div className="admin-test-list">{detail.tests.length ? detail.tests.map((test) => <AdminTestRow test={test} key={test.id} />) : <div className="admin-empty compact"><FlaskConical size={21} /><strong>Тестов пока нет</strong><span>У этого магазина ещё не запускались эксперименты.</span></div>}</div></section></div>; }
function AdminTestRow({ test }: { test: AdminTestItem }) { return <div className="admin-test-row"><div><strong>{test.title}</strong><span>#{test.id} · Артикул {test.nm_id} {test.wb_campaign_id ? `· Кампания ${test.wb_campaign_id}` : ''}</span></div><div className="admin-test-stats"><span className={`admin-test-status ${test.status}`}>{statusLabel(test.status)}</span><small>{number(test.total_views)} показов · {number(test.total_clicks)} кликов · {rub(test.total_spend_rub)}</small></div></div>; }
