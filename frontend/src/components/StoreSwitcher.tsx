import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Check, ChevronDown, KeyRound, Store as StoreIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { WBConnection } from '../types';

type StoreSwitcherProps = {
  connections: WBConnection[];
  activeConnection: WBConnection;
  onSelect: (connectionId: number) => void;
};

export function StoreSwitcher({ connections, activeConnection, onSelect }: StoreSwitcherProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, []);

  const select = (connectionId: number) => {
    onSelect(connectionId);
    setOpen(false);
  };

  return <div className="store-switcher" ref={rootRef}>
    <button type="button" className={`store-switcher-trigger ${open ? 'open' : ''}`} onClick={() => setOpen((value) => !value)} aria-haspopup="listbox" aria-expanded={open}>
      <span className="store-switcher-icon"><StoreIcon size={17} /></span>
      <span className="store-switcher-trigger-copy"><small>Активный магазин</small><strong>{activeConnection.store_name}</strong></span>
      <span className={`status-dot ${activeConnection.ready_for_ab_tests ? 'ready' : 'warning'}`} />
      <ChevronDown size={17} className="store-switcher-chevron" />
    </button>
    {open && <div className="store-switcher-menu" role="listbox" aria-label="Выбор магазина">
      <div className="store-switcher-menu-heading"><div><strong>Ваши магазины</strong><span>Выберите рабочее пространство</span></div><b>{connections.length}</b></div>
      <div className="store-switcher-options">{connections.map((item) => <button type="button" role="option" aria-selected={item.id === activeConnection.id} className={`store-switcher-option ${item.id === activeConnection.id ? 'active' : ''}`} key={item.id} onClick={() => select(item.id)}><span className="store-switcher-option-icon"><StoreIcon size={16} /></span><span className="store-switcher-option-copy"><strong>{item.store_name}</strong><small>Токен ••••{item.token_last4}</small></span><span className={`store-switcher-state ${item.ready_for_ab_tests ? 'ready' : 'warning'}`}><span className={`status-dot ${item.ready_for_ab_tests ? 'ready' : 'warning'}`} />{item.ready_for_ab_tests ? 'Готово' : 'Проверить'}</span>{item.id === activeConnection.id && <Check size={16} className="store-switcher-selected" />}</button>)}</div>
      <Link to="/settings" className="store-switcher-settings" onClick={() => setOpen(false)}><KeyRound size={15} /> Настройки магазинов <ArrowUpRight size={14} /></Link>
    </div>}
  </div>;
}
