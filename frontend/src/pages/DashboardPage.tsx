import { useEffect, useMemo, useState } from 'react';
import { ArrowUpRight, BarChart3, CheckCircle2, CircleAlert, Eye, EyeOff, FlaskConical, KeyRound, MousePointer2, Percent, RefreshCw, ShieldCheck, ShoppingBag, Sparkles, TrendingUp, WalletCards, X } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { AppShell } from '../components/AppShell';
import { StoreSwitcher } from '../components/StoreSwitcher';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import type { DashboardStats, WBConnection } from '../types';
import { publishActiveStoreId, readActiveStoreId, subscribeActiveStoreId } from '../utils/activeStore';
const PERIODS = [['today', 'Сегодня'], ['week', '7 дней'], ['month', '30 дней'], ['custom', 'Период']] as const;
function formatNumber(value: number) { return new Intl.NumberFormat('ru-RU').format(Math.round(value || 0)); }
function formatRub(value: number) { return `${new Intl.NumberFormat('ru-RU').format(Math.round(value || 0))} ₽`; }
function formatCompactRub(value: number | null) { return value == null ? '—' : formatRub(value); }
function formatPercent(value: number) { return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value || 0)}%`; }
function periodLabel(period: string) { return period === 'today' ? 'сегодня' : period === 'week' ? '7 дней' : period === 'month' ? '30 дней' : 'выбранный период'; }

export function DashboardPage() {
  const { user } = useAuth();
  const [connections, setConnections] = useState<WBConnection[]>([]);
  const [activeConnectionId, setActiveConnectionId] = useState<number | null>(() => {
    return readActiveStoreId();
  });
  const [loading, setLoading] = useState(true);
  const [validating, setValidating] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const [showTokenModal, setShowTokenModal] = useState(false);
  const [token, setToken] = useState('');
  const [storeName, setStoreName] = useState('Мой магазин');
  const [tokenBusy, setTokenBusy] = useState(false);
  const [tokenError, setTokenError] = useState('');
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [period, setPeriod] = useState<'today' | 'week' | 'month' | 'custom'>('today');
  const [customBegin, setCustomBegin] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const [statsRefresh, setStatsRefresh] = useState(0);

  const connection = useMemo(
    () => connections.find((item) => item.id === activeConnectionId) || connections[0] || null,
    [activeConnectionId, connections],
  );

  const load = async () => {
    try {
      const result = await api.getWBConnections();
      setConnections(result);
      const selected = result.find((item) => item.id === readActiveStoreId()) || result[0];
      if (selected) {
        setActiveConnectionId(selected.id);
        publishActiveStoreId(selected.id);
      } else {
        setActiveConnectionId(null);
        publishActiveStoreId(null);
      }
      setStatsRefresh((value) => value + 1);
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось загрузить магазины WB');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);
  useEffect(() => subscribeActiveStoreId((id) => setActiveConnectionId(id)), []);
  useEffect(() => {
    // Dashboard uses only local experiment totals. An optional WB permission
    // refresh must not hide already saved A/B-test results.
    if (!connection) { setStats(null); return; }
    if (period === 'custom' && (!customBegin || !customEnd)) { setStats(null); return; }
    let active = true;
    setStatsLoading(true);
    void api.getDashboardStats(connection.id, period, period === 'custom' ? customBegin : undefined, period === 'custom' ? customEnd : undefined)
      .then((result) => { if (active) setStats(result); })
      .catch((error: any) => { if (active) toast.error(error?.message || 'Не удалось загрузить статистику рекламы'); })
      .finally(() => { if (active) setStatsLoading(false); });
    return () => { active = false; };
  }, [connection?.id, period, customBegin, customEnd, statsRefresh]);
  useEffect(() => {
    if (!loading && searchParams.get('onboarding') === '1' && !connection) setShowTokenModal(true);
  }, [connection, loading, searchParams]);

  const selectConnection = (connectionId: number) => {
    setActiveConnectionId(connectionId);
    publishActiveStoreId(connectionId);
  };

  const replaceConnection = (updated: WBConnection) => {
    setConnections((current) => current.map((item) => item.id === updated.id ? updated : item));
  };

  const validate = async () => {
    if (!connection) return;
    setValidating(true);
    try {
      replaceConnection(await api.validateWB(connection.id));
      toast.success('Доступ WB проверен');
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось проверить токен WB');
    } finally {
      setValidating(false);
    }
  };

  const closeTokenModal = () => {
    setShowTokenModal(false);
    setToken('');
    setStoreName('Мой магазин');
    setTokenError('');
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete('onboarding');
    setSearchParams(nextParams, { replace: true });
  };

  const connectOnboardingToken = async (event: React.FormEvent) => {
    event.preventDefault();
    setTokenError('');
    setTokenBusy(true);
    try {
      const result = await api.connectWB(storeName, token, true);
      setConnections((current) => [...current, result]);
      selectConnection(result.id);
      toast.success('Магазин подключён. Доступы для A/B-тестов подтверждены.');
      closeTokenModal();
    } catch (error: any) {
      setTokenError(error?.message || 'Не удалось проверить токен WB');
    } finally {
      setTokenBusy(false);
    }
  };

  const ready = connection?.ready_for_ab_tests;
  return <AppShell>
    <div className="page-wrap">
      <div className="page-heading">
        <div>
          <div className="eyebrow eyebrow-dark"><Sparkles size={14} /> Личное пространство</div>
          <h1>Доброе утро, {user?.first_name || 'продавец'}.</h1>
          <p>Главные показатели ваших A/B-тестов за {periodLabel(period)}.</p>
        </div>
        <div className="page-heading-actions">
          {connection ? <StoreSwitcher connections={connections} activeConnection={connection} onSelect={selectConnection} /> : <Link to="/settings" className="outline-button"><KeyRound size={16} /> Подключить WB <ArrowUpRight size={15} /></Link>}
        </div>
      </div>
      <section className="welcome-banner"><div className="welcome-glow" /><div className="welcome-copy"><span className="mini-label">WB OPTIMIZER / 01</span><h2>Превратите каталог<br /><em>в источник роста.</em></h2><p>Подключите аккаунт Wildberries, чтобы менять изображения карточек, запускать продвижение и проводить A/B-тесты.</p><Link to="/settings" className="light-button">Открыть настройки <ArrowUpRight size={15} /></Link></div><div className="welcome-illustration"><div className="float-card float-card-main"><div className="float-card-top"><span className="tiny-icon"><BarIcon /></span><span>Рост тестов</span><strong>+24,8%</strong></div><div className="mini-bars"><i /><i /><i /><i /><i /><i /><i /></div></div><div className="float-card float-card-small"><CheckCircle2 size={15} /><span>Все системы<br /><strong>готовы</strong></span></div></div></section>
      <div className="section-heading"><div><h3>Обзор пространства</h3><p>{connection ? `Показатели A/B-тестов магазина «${connection.store_name}»` : 'Показатели появятся после запуска A/B-теста.'}</p></div><div className="dashboard-periods">{PERIODS.map(([value, label]) => <button key={value} className={period === value ? 'active' : ''} onClick={() => setPeriod(value)}>{label}</button>)}<button className="icon-button" onClick={() => void load()} aria-label="Обновить"><RefreshCw size={16} /></button></div></div>
      {period === 'custom' && <div className="dashboard-custom-period"><label>С <input type="date" value={customBegin} onChange={(event) => setCustomBegin(event.target.value)} /></label><label>По <input type="date" value={customEnd} onChange={(event) => setCustomEnd(event.target.value)} /></label></div>}
      <section className="metric-grid dashboard-metric-grid">
        <Metric icon={<FlaskConical />} label="Всего тестов" value={statsLoading ? '…' : stats ? formatNumber(stats.campaign_count) : '—'} hint={stats ? `За ${periodLabel(stats.period)}` : 'Появятся после запуска теста'} />
        <Metric icon={<TrendingUp />} label="Активные тесты" value={statsLoading ? '…' : stats ? formatNumber(stats.active_campaign_count) : '—'} hint={stats ? `${formatNumber(stats.completed_campaign_count)} завершено` : 'Пока нет активных тестов'} />
        <Metric icon={<Eye />} label="Показы" value={statsLoading ? '…' : stats ? formatNumber(stats.views) : '—'} hint={stats ? 'Накоплено в A/B-тестах' : 'Появятся после запуска теста'} />
        <Metric icon={<MousePointer2 />} label="Клики" value={statsLoading ? '…' : stats ? formatNumber(stats.clicks) : '—'} hint={stats ? 'По тестам этого магазина' : 'Появятся после запуска теста'} />
        <Metric icon={<Percent />} label="CTR тестов" value={statsLoading ? '…' : stats ? formatPercent(stats.ctr) : '—'} hint={stats ? 'Клики ÷ показы' : 'Рассчитается после показов'} />
        <Metric icon={<WalletCards />} label="Расход" value={statsLoading ? '…' : stats ? formatRub(stats.spend_rub) : '—'} hint={stats ? 'Только расходы наших тестов' : 'Появятся после запуска теста'} />
        <Metric icon={<ShoppingBag />} label="CPO" value={statsLoading ? '…' : stats ? formatCompactRub(stats.cpo_rub) : '—'} hint={stats?.orders ? `${formatNumber(stats.orders)} заказов` : 'Заказы пока не зафиксированы'} />
        <Metric icon={<ShieldCheck />} label="Готовность к A/B-тестам" value={loading ? '…' : ready ? 'Готово' : 'Настроить'} hint={ready ? 'Контент и Продвижение доступны' : 'Нужен токен WB с двумя доступами'} accent={Boolean(ready)} />
      </section>
      <section className="dashboard-insights-grid">
        <section className="dashboard-chart-card">
          <div className="dashboard-card-heading"><div><span className="settings-kicker">ДИНАМИКА ТЕСТОВ</span><h3>Результаты по датам запуска</h3><p>Показы и клики из последних сохранённых данных ваших экспериментов.</p></div><div className="dashboard-chart-legend"><span><i className="chart-dot views" />Показы</span><span><i className="chart-dot clicks" />Клики</span></div></div>
          {statsLoading ? <div className="dashboard-chart-empty"><BarChart3 size={22} /><strong>Загружаем данные…</strong></div> : stats?.chart.length ? <DashboardChart points={stats.chart} /> : <div className="dashboard-chart-empty"><BarChart3 size={22} /><strong>Данных для графика пока нет</strong><span>Запустите A/B-тест — его результаты появятся здесь.</span></div>}
        </section>
        <section className="dashboard-summary-card"><div><span className="settings-kicker">СОСТОЯНИЕ ПРОСТРАНСТВА</span><h3>Коротко о результатах</h3><p>Локальные показатели выбранного магазина</p></div><div className="dashboard-summary-list"><SummaryRow label="Завершено тестов" value={stats ? formatNumber(stats.completed_campaign_count) : '—'} /><SummaryRow label="Остановлено с ошибкой" value={stats ? formatNumber(stats.failed_campaign_count) : '—'} warning={Boolean(stats?.failed_campaign_count)} /><SummaryRow label="Заказы" value={stats ? formatNumber(stats.orders) : '—'} /><SummaryRow label="Средний расход на заказ" value={stats ? formatCompactRub(stats.cpo_rub) : '—'} /></div><Link to="/ab-tests" className="dashboard-summary-link">Открыть A/B-тесты <ArrowUpRight size={14} /></Link></section>
      </section>
      <section className="connection-card"><div className="connection-icon"><ShieldCheck size={21} /></div><div className="connection-main"><div className="connection-title"><h3>{connection ? `Магазин «${connection.store_name}»` : 'Подключение Wildberries'}</h3>{connection?.connected && <span className={`status-pill ${ready ? 'status-ready' : 'status-warning'}`}><span />{ready ? 'Готово к A/B-тестам' : 'Требуется действие'}</span>}</div>{loading ? <p>Загрузка статуса подключения…</p> : connection?.connected ? <p>Токен заканчивается на <strong>••••{connection.token_last4}</strong> · Последняя проверка: {connection.last_validated_at ? new Date(connection.last_validated_at).toLocaleString('ru-RU') : 'ещё не выполнялась'}</p> : <p>У вас пока нет подключённых магазинов. Добавьте токен API WB, чтобы активировать рабочее пространство.</p>}</div><div className="connection-actions">{connection?.connected && <button className="outline-button compact" onClick={() => void validate()} disabled={validating}>{validating ? 'Проверка…' : 'Проверить доступ'}<RefreshCw size={15} /></button>}<Link to="/settings" className="primary-button compact">{connection?.connected ? 'Подробнее' : 'Подключить сейчас'}<ArrowUpRight size={15} /></Link></div></section>
      {!connection && <div className="setup-callout"><CircleAlert size={18} /><div><strong>Пространство почти готово</strong><p>Подключите токен WB, чтобы запускать A/B-тесты карточек.</p></div><Link to="/settings">Настроить сейчас <ArrowUpRight size={14} /></Link></div>}
      {showTokenModal && <WBTokenModal token={token} setToken={setToken} storeName={storeName} setStoreName={setStoreName} busy={tokenBusy} error={tokenError} onSubmit={connectOnboardingToken} onClose={closeTokenModal} />}
    </div>
  </AppShell>;
}

function Metric({ icon, label, value, hint, accent = false }: { icon: React.ReactNode; label: string; value: string; hint: string; accent?: boolean }) {
  return <div className={`metric-card ${accent ? 'metric-card-accent' : ''}`}><div className="metric-icon">{icon}</div><span>{label}</span><strong>{value}</strong><small>{hint}</small></div>;
}

function SummaryRow({ label, value, warning = false }: { label: string; value: string; warning?: boolean }) {
  return <div className="dashboard-summary-row"><span>{label}</span><strong className={warning ? 'warning-value' : ''}>{value}</strong></div>;
}

function DashboardChart({ points }: { points: DashboardStats['chart'] }) {
  const width = 760;
  const height = 250;
  const left = 42;
  const right = 18;
  const top = 18;
  const bottom = 30;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const maxViews = Math.max(1, ...points.map((point) => point.views));
  const maxClicks = Math.max(1, ...points.map((point) => point.clicks));
  const x = (index: number) => points.length === 1 ? left + plotWidth / 2 : left + (index / (points.length - 1)) * plotWidth;
  const y = (value: number, max: number) => top + plotHeight - (value / max) * plotHeight;
  const viewLine = points.map((point, index) => `${x(index)},${y(point.views, maxViews)}`).join(' ');
  const clickLine = points.map((point, index) => `${x(index)},${y(point.clicks, maxClicks)}`).join(' ');
  return <div className="dashboard-chart"><div className="dashboard-chart-plot"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Показы и клики по датам запуска тестов"><defs><linearGradient id="dashboardViewsFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="#4d7ff0" stopOpacity=".18" /><stop offset="1" stopColor="#4d7ff0" stopOpacity="0" /></linearGradient></defs>{[0, .25, .5, .75, 1].map((ratio) => <line key={ratio} x1={left} x2={width - right} y1={top + plotHeight * ratio} y2={top + plotHeight * ratio} className="chart-grid-line" />)}<polygon points={`${left},${top + plotHeight} ${viewLine} ${left + plotWidth},${top + plotHeight}`} className="chart-area" /> <polyline points={viewLine} className="chart-line chart-line-views" fill="none" /> <polyline points={clickLine} className="chart-line chart-line-clicks" fill="none" />{points.map((point, index) => <g key={point.date}><circle cx={x(index)} cy={y(point.views, maxViews)} r="4" className="chart-point chart-point-views" /><circle cx={x(index)} cy={y(point.clicks, maxClicks)} r="3.5" className="chart-point chart-point-clicks" /></g>)}</svg></div><div className="dashboard-chart-axis">{points.map((point) => <span key={point.date}>{formatChartDate(point.date)}</span>)}</div></div>;
}

function formatChartDate(value: string) {
  return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: 'short' }).format(new Date(`${value}T00:00:00`)).replace('.', '');
}

function BarIcon() { return <span className="bar-icon"><i /><i /><i /></span>; }

function WBTokenModal({ token, setToken, storeName, setStoreName, busy, error, onSubmit, onClose }: { token: string; setToken: (value: string) => void; storeName: string; setStoreName: (value: string) => void; busy: boolean; error: string; onSubmit: (event: React.FormEvent) => void; onClose: () => void }) {
  const [showToken, setShowToken] = useState(false);
  return <div className="token-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="token-modal" role="dialog" aria-modal="true" aria-labelledby="token-modal-title"><button type="button" className="icon-button token-modal-close" onClick={onClose} aria-label="Закрыть"><X size={17} /></button><div className="token-modal-icon"><ShieldCheck size={22} /></div><span className="modal-kicker">ПОСЛЕДНИЙ ШАГ</span><h2 id="token-modal-title">Подключите Wildberries</h2><p>Добавьте магазин и токен API, чтобы менять карточки, запускать продвижение и проводить A/B-тесты.</p><form onSubmit={onSubmit} className="token-modal-form"><label>Название магазина<input value={storeName} onChange={(event) => setStoreName(event.target.value)} placeholder="Мой магазин" required minLength={2} maxLength={120} /></label><div className="token-requirements"><div><CheckCircle2 size={16} /><span><strong>Контент</strong><small>Доступ к изменению изображений карточки</small></span></div><div><CheckCircle2 size={16} /><span><strong>Продвижение</strong><small>Доступ к запуску рекламной кампании</small></span></div></div><label>Токен WB API<div className="token-input"><input type={showToken ? 'text' : 'password'} value={token} onChange={(event) => setToken(event.target.value)} placeholder="Вставьте токен API Wildberries" required minLength={20} autoComplete="off" autoFocus /><button type="button" onClick={() => setShowToken((value) => !value)} aria-label={showToken ? 'Скрыть токен' : 'Показать токен'}>{showToken ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label>{error && <div className="token-modal-error"><CircleAlert size={16} /><span>{error}</span></div>}<button className="primary-button" disabled={busy}>{busy ? 'Проверка токена…' : 'Проверить и подключить'}<ShieldCheck size={17} /></button><button type="button" className="modal-skip" onClick={onClose}>Пропустить пока</button></form></section></div>;
}
