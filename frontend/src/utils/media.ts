const API_ORIGIN = (import.meta.env.VITE_API_URL || 'http://localhost:8000/api').replace(/\/api\/?$/, '');
const SESSION_CACHE_KEY = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

/** Keep WB/CDN previews fresh without changing the URL on every React render. */
export function imageUrl(value?: string | null, scope = ''): string {
  if (!value) return '';
  const resolved = value.startsWith('/') ? `${API_ORIGIN}${value}` : value;
  if (!/^https?:\/\//i.test(resolved)) return resolved;

  try {
    const url = new URL(resolved);
    url.searchParams.set('wb_optimizer_preview', `${SESSION_CACHE_KEY}${scope ? `-${scope}` : ''}`);
    return url.toString();
  } catch {
    return resolved;
  }
}
