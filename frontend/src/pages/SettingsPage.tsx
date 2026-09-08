import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, ArrowUpRight, CheckCircle2, Eye, EyeOff, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { AppShell } from '../components/AppShell';
import { api } from '../api/client';
import type { WBConnection } from '../types';
import { publishActiveStoreId, readActiveStoreId, subscribeActiveStoreId } from '../utils/activeStore';

export function SettingsPage() {
  const [connections, setConnections] = useState<WBConnection[]>([]);
  const [activeConnectionId, setActiveConnectionId] = useState<number | null>(() => {
    return readActiveStoreId();
  });
  const [token, setToken] = useState('');
  const [storeName, setStoreName] = useState('');
  const [showToken, setShowToken] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [validating, setValidating] = useState(false);

  const connection = useMemo(
    () => connections.find((item) => item.id === activeConnectionId) || connections[0] || null,
    [activeConnectionId, connections],
  );

  const loadConnections = async () => {
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
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось загрузить магазины');
    }
  };

  useEffect(() => { void loadConnections(); }, []);
  useEffect(() => subscribeActiveStoreId((id) => setActiveConnectionId(id)), []);

  const selectConnection = (connectionId: number) => {
    setActiveConnectionId(connectionId);
    publishActiveStoreId(connectionId);
  };

  const addConnection = async (event: React.FormEvent) => {
    event.preventDefault();
    setConnecting(true);
    try {
      const result = await api.connectWB(storeName, token);
      setConnections((current) => [...current, result]);
      selectConnection(result.id);
      setToken('');
      setStoreName('');
      toast.success(result.ready_for_ab_tests ? 'Магазин подключён — A/B-тесты готовы' : 'Магазин подключён с ограниченным доступом');
    } catch (error: any) {
      const detail = error?.details?.detail;
      toast.error(typeof detail === 'string' ? detail : error?.message || 'Не удалось подключить магазин');
    } finally {
      setConnecting(false);
    }
  };

  const replaceConnection = (updated: WBConnection) => {
    setConnections((current) => current.map((item) => item.id === updated.id ? updated : item));
  };

  const validate = async () => {
    if (!connection) return;
    setValidating(true);
    try {
      replaceConnection(await api.validateWB(connection.id));
      toast.success('Доступ WB обновлён');
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось проверить токен');
    } finally {
      setValidating(false);
    }
  };

  const disconnect = async () => {
    if (!connection || !window.confirm(`Удалить магазин «${connection.store_name}»?`)) return;
    try {
      await api.disconnectWB(connection.id);
      const remaining = connections.filter((item) => item.id !== connection.id);
      setConnections(remaining);
      const next = remaining[0];
      setActiveConnectionId(next?.id || null);
      publishActiveStoreId(next?.id || null);
      toast.success('Магазин удалён');
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось удалить магазин');
    }
  };

  return <AppShell>
    <div className="page-wrap settings-page">
      <div className="page-heading"><div><div className="eyebrow eyebrow-dark"><KeyRound size={14} /> НАСТРОЙКИ МАГАЗИНОВ</div><h1>Настройки магазинов</h1><p>Подключайте магазины Wildberries и управляйте доступами в одном месте.</p></div><Link to="/dashboard" className="outline-button">Перейти к обзору <ArrowUpRight size={15} /></Link></div>
      <div className="settings-content settings-content-wide">
        <section className="settings-card stores-card"><div className="settings-card-heading"><div><span className="settings-kicker">ПОДКЛЮЧЕНИЯ</span><h2>Магазины Wildberries</h2><p>У каждого магазина свой токен, доступы и статистика.</p></div><div className="large-status-icon"><KeyRound size={22} /></div></div>{connections.length > 0 ? <div className="store-list">{connections.map((item) => <button type="button" className={`store-list-item ${item.id === connection?.id ? 'active' : ''}`} key={item.id} onClick={() => selectConnection(item.id)}><span className="store-list-icon"><StoreIcon /></span><span className="store-list-copy"><strong>{item.store_name}</strong><small>Токен заканчивается на ••••{item.token_last4}</small></span><span className={`status-dot ${item.ready_for_ab_tests ? 'ready' : 'warning'}`} /></button>)}</div> : <div className="stores-empty"><span className="store-empty-icon"><KeyRound size={20} /></span><div><strong>Магазины ещё не подключены</strong><p>Добавьте первый токен WB, чтобы открыть доступ к продвижению и A/B-тестам.</p></div></div>}<form onSubmit={addConnection} className="store-add-form"><div className="store-form-grid"><label>Название магазина<input value={storeName} onChange={(event) => setStoreName(event.target.value)} placeholder="Например, Основной магазин" required minLength={2} maxLength={120} /></label><label>Токен WB API<div className="token-input"><input type={showToken ? 'text' : 'password'} value={token} onChange={(event) => setToken(event.target.value)} placeholder="Вставьте токен API Wildberries" required minLength={20} autoComplete="off" /><button type="button" onClick={() => setShowToken((value) => !value)} aria-label={showToken ? 'Скрыть токен' : 'Показать токен'}>{showToken ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label></div><div className="token-help"><AlertCircle size={15} /><span>У токена должен быть доступ к категориям <strong>«Контент»</strong> и <strong>«Продвижение»</strong>. Перед сохранением токен шифруется.</span></div><button className="primary-button" disabled={connecting}>{connecting ? 'Проверка доступа WB…' : 'Добавить магазин и проверить'}<Plus size={17} /></button></form></section>
        {connection && <section className="settings-card wb-settings-card"><div className="settings-card-heading"><div><span className="settings-kicker">АКТИВНЫЙ МАГАЗИН</span><h2>{connection.store_name}</h2><p>Доступы и состояние подключения выбранного магазина.</p></div><div className={`large-status-icon ${connection.ready_for_ab_tests ? 'ready' : ''}`}>{connection.ready_for_ab_tests ? <ShieldCheck size={22} /> : <KeyRound size={22} />}</div></div><div className="connected-banner"><div><span className={`status-pill ${connection.ready_for_ab_tests ? 'status-ready' : 'status-warning'}`}><span /> {connection.ready_for_ab_tests ? 'Готово к A/B-тестам' : 'Ограниченный доступ'}</span><strong>Токен заканчивается на ••••{connection.token_last4}</strong></div><button className="danger-button" onClick={() => void disconnect()}><Trash2 size={15} /> Удалить</button></div><AccessGrid connection={connection} /><div className="validate-row"><span><RefreshCw size={14} /> {connection.last_validated_at ? `Последняя проверка: ${new Date(connection.last_validated_at).toLocaleString('ru-RU')}` : 'Проверка ещё не выполнялась'}</span><button className="outline-button compact" onClick={() => void validate()} disabled={validating}>{validating ? 'Проверка…' : 'Проверить доступ'}<RefreshCw size={15} /></button></div></section>}
      </div>
    </div>
  </AppShell>;
}

function StoreIcon() { return <span className="store-mini-icon"><span /><span /><span /></span>; }

function AccessGrid({ connection }: { connection: WBConnection }) {
  const items = [['content', 'Контент'], ['promotion', 'Продвижение']] as const;
  return <div className="access-grid">{items.map(([key, label]) => { const allowed = connection.access[key]; return <div className={`access-item ${allowed ? 'allowed' : 'denied'}`} key={key}>{allowed ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}<div><strong>{label}</strong><span>{allowed ? 'Доступ подтверждён' : 'Доступ не найден'}</span></div></div>; })}</div>;
}
