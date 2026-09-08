export const ACTIVE_STORE_KEY = 'wb_optimizer_active_store_id';
const ACTIVE_STORE_EVENT = 'wb_optimizer_active_store_changed';

function normalize(value: number | string | null | undefined): number | null {
  const id = Number(value);
  return Number.isInteger(id) && id > 0 ? id : null;
}

export function readActiveStoreId(): number | null {
  return normalize(localStorage.getItem(ACTIVE_STORE_KEY));
}

/**
 * localStorage fires `storage` only in other tabs. The custom event keeps
 * components in the same tab in sync as well, without forcing a full reload.
 */
export function publishActiveStoreId(value: number | null): void {
  if (value == null) localStorage.removeItem(ACTIVE_STORE_KEY);
  else localStorage.setItem(ACTIVE_STORE_KEY, String(value));
  window.dispatchEvent(new CustomEvent<number | null>(ACTIVE_STORE_EVENT, { detail: value }));
}

export function subscribeActiveStoreId(onChange: (id: number | null) => void): () => void {
  const handleStorage = (event: StorageEvent) => {
    if (event.key === ACTIVE_STORE_KEY) onChange(normalize(event.newValue));
  };
  const handleLocal = (event: Event) => {
    onChange(normalize((event as CustomEvent<number | null>).detail));
  };
  window.addEventListener('storage', handleStorage);
  window.addEventListener(ACTIVE_STORE_EVENT, handleLocal);
  return () => {
    window.removeEventListener('storage', handleStorage);
    window.removeEventListener(ACTIVE_STORE_EVENT, handleLocal);
  };
}
