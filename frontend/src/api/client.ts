import type { ABTest, ABTestCard, ABTestCardCursor, ABTestCardsPage, ABTestCreatePayload, AdminAuditItem, AdminDashboard, AdminSettings, AdminStoreDetail, AdminStoreItem, AdminUserDetail, AdminUserItem, DashboardStats, TokenResponse, User, WBConnection, WBPromotionBalance } from '../types';

const API_URL = (import.meta.env.VITE_API_URL || 'http://localhost:8000/api').replace(/\/$/, '');
const ACCESS_KEY = 'wb_optimizer_access_token';
const REFRESH_KEY = 'wb_optimizer_refresh_token';

export class ApiError extends Error {
  status: number;
  details: unknown;

  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

export function getStoredAccessToken() {
  return localStorage.getItem(ACCESS_KEY);
}

function saveTokens(tokens: TokenResponse) {
  localStorage.setItem(ACCESS_KEY, tokens.access_token);
  localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
}

export function clearTokens() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

async function readError(response: Response): Promise<{ message: string; details: unknown }> {
  const fallbackMessages: Record<number, string> = {
    400: 'Некорректный запрос',
    401: 'Требуется авторизация',
    403: 'Доступ запрещён',
    404: 'Ресурс не найден',
    409: 'Данные конфликтуют с существующими',
    422: 'Проверьте введённые данные',
    429: 'Слишком много запросов. Попробуйте позже',
    500: 'Внутренняя ошибка сервера',
    503: 'Сервис временно недоступен',
  };
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === 'string') return { message: detail, details: body };
    if (detail?.message) return { message: detail.message, details: body };
    if (Array.isArray(detail)) {
      const labels: Record<string, string> = {
        email: 'Электронная почта', password: 'Пароль', confirm_password: 'Подтверждение пароля',
        first_name: 'Имя', last_name: 'Фамилия', code: 'Код', token: 'Токен WB',
      };
      const messages = detail.map((item) => {
        const location = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : '';
        let message = String(item?.msg || 'Проверьте значение');
        message = message.replace(/^Value error,\s*/i, '');
        if (/valid email/i.test(message)) message = 'Введите корректную электронную почту';
        else if (/at least (\d+) characters?/i.test(message)) message = `Введите не менее ${message.match(/at least (\d+)/i)?.[1]} символов`;
        else if (/at most (\d+) characters?/i.test(message)) message = `Введите не более ${message.match(/at most (\d+)/i)?.[1]} символов`;
        else if (/field required/i.test(message)) message = 'Заполните обязательное поле';
        else if (/greater than/i.test(message)) message = 'Значение должно быть больше нуля';
        return `${labels[String(location)] || ''}${labels[String(location)] ? ': ' : ''}${message}`;
      });
      return { message: messages.filter(Boolean).join('. ') || 'Проверьте введённые данные', details: body };
    }
    return { message: body?.message || 'Не удалось выполнить запрос', details: body };
  } catch {
    return { message: fallbackMessages[response.status] || 'Не удалось выполнить запрос', details: undefined };
  }
}

async function refreshTokens(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = refreshTokensOnce();
  try { return await refreshInFlight; } finally { refreshInFlight = null; }
}

let refreshInFlight: Promise<boolean> | null = null;

async function refreshTokensOnce(): Promise<boolean> {
  const refreshToken = localStorage.getItem(REFRESH_KEY);
  if (!refreshToken) return false;
  const response = await fetch(`${API_URL}/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!response.ok) {
    clearTokens();
    return false;
  }
  saveTokens(await response.json() as TokenResponse);
  return true;
}

async function request<T>(path: string, init: RequestInit = {}, canRefresh = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const token = getStoredAccessToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const response = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (response.status === 401 && canRefresh && !path.includes('/auth/')) {
    if (await refreshTokens()) return request<T>(path, init, false);
  }
  if (!response.ok) {
    const error = await readError(response);
    throw new ApiError(error.message, response.status, error.details);
  }
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
}

export const api = {
  register: (payload: { email: string; password: string; confirm_password: string; first_name: string; last_name: string }) =>
    request<{ message: string; email: string; expires_in: number }>('/auth/register/start', { method: 'POST', body: JSON.stringify(payload) }),
  verifyEmail: async (email: string, code: string) => {
    const tokens = await request<TokenResponse>('/auth/register/verify', { method: 'POST', body: JSON.stringify({ email, code }) });
    saveTokens(tokens);
    return tokens;
  },
  resendCode: (email: string) =>
    request<{ message: string; email: string; expires_in: number }>('/auth/register/resend', { method: 'POST', body: JSON.stringify({ email }) }),
  login: async (email: string, password: string) => {
    const tokens = await request<TokenResponse>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) });
    saveTokens(tokens);
    return tokens;
  },
  logout: async () => {
    const refresh_token = localStorage.getItem(REFRESH_KEY);
    if (refresh_token) {
      try { await request('/auth/logout', { method: 'POST', body: JSON.stringify({ refresh_token }) }, false); } catch { /* local logout still wins */ }
    }
    clearTokens();
  },
  me: () => request<User>('/auth/me'),
  updateProfile: (first_name: string, last_name: string) =>
    request<User>('/auth/profile', { method: 'PATCH', body: JSON.stringify({ first_name, last_name }) }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ message: string }>('/auth/change-password', { method: 'POST', body: JSON.stringify({ current_password, new_password }) }),
  forgotPassword: (email: string) =>
    request<{ message: string }>('/auth/forgot-password', { method: 'POST', body: JSON.stringify({ email }) }),
  resetPassword: (email: string, code: string, new_password: string) =>
    request<{ message: string }>('/auth/reset-password', { method: 'POST', body: JSON.stringify({ email, code, new_password }) }),
  getAdminDashboard: () => request<AdminDashboard>('/admin/dashboard'),
  getAdminUsers: (search = '', page = 1, pageSize = 20) => {
    const params = new URLSearchParams({ search, page: String(page), page_size: String(pageSize) });
    return request<{ items: AdminUserItem[]; total: number; page: number; page_size: number }>(`/admin/users?${params.toString()}`);
  },
  getAdminUser: (userId: number) => request<AdminUserDetail>(`/admin/users/${userId}`),
  createAdminUser: (payload: { email: string; password: string; confirm_password: string; first_name: string; last_name: string; is_verified: boolean; is_active: boolean; is_admin: boolean }) =>
    request<AdminUserItem>('/admin/users', { method: 'POST', body: JSON.stringify(payload) }),
  updateAdminUser: (userId: number, payload: Partial<Pick<AdminUserItem, 'email' | 'first_name' | 'last_name' | 'is_verified' | 'is_active' | 'is_admin'>>) =>
    request<AdminUserItem>(`/admin/users/${userId}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  changeAdminUserPassword: (userId: number, password: string, confirm_password: string) =>
    request<{ message: string }>(`/admin/users/${userId}/password`, { method: 'POST', body: JSON.stringify({ password, confirm_password }) }),
  deleteAdminUser: (userId: number) => request<void>(`/admin/users/${userId}`, { method: 'DELETE' }),
  getAdminStores: (search = '') => request<AdminStoreItem[]>(`/admin/stores?${new URLSearchParams({ search }).toString()}`),
  getAdminStore: (storeId: number) => request<AdminStoreDetail>(`/admin/stores/${storeId}`),
  getAdminSettings: () => request<AdminSettings>('/admin/settings'),
  updateAdminSettings: (registration_enabled: boolean) => request<AdminSettings>('/admin/settings', { method: 'PATCH', body: JSON.stringify({ registration_enabled }) }),
  getAdminAudit: (limit = 100) => request<{ items: AdminAuditItem[]; total: number }>(`/admin/audit?limit=${limit}`),
  getWBConnections: () => request<WBConnection[]>('/wb/connections'),
  getDashboardStats: (connectionId: number, period = 'today', beginDate?: string, endDate?: string) => {
    const params = new URLSearchParams({ connection_id: String(connectionId), period });
    if (beginDate) params.set('begin_date', beginDate);
    if (endDate) params.set('end_date', endDate);
    return request<DashboardStats>(`/wb/dashboard?${params.toString()}`);
  },
  getWBPromotionBalance: (connectionId: number) => request<WBPromotionBalance>(`/wb/promotion/balance?connection_id=${connectionId}`),
  getWBConnection: (connectionId?: number) => request<WBConnection>(connectionId ? `/wb/token?connection_id=${connectionId}` : '/wb/token'),
  connectWB: (store_name: string, token: string, requireAbTestAccess = false) => request<WBConnection>(`/wb/connections${requireAbTestAccess ? '?require_ab_test_access=true' : ''}`, { method: 'POST', body: JSON.stringify({ store_name, token }) }),
  validateWB: (connectionId: number) => request<WBConnection>(`/wb/connections/${connectionId}/validate`, { method: 'POST' }),
  disconnectWB: (connectionId: number) => request<void>(`/wb/connections/${connectionId}`, { method: 'DELETE' }),
  getABTestCards: (connectionId: number, search = '', signal?: AbortSignal, cursor?: ABTestCardCursor) => {
    const params = new URLSearchParams({ connection_id: String(connectionId), search });
    if (cursor?.updatedAt) params.set('cursor_updated_at', cursor.updatedAt);
    if (cursor?.nmID || cursor?.nmId) params.set('cursor_nm_id', String(cursor.nmID || cursor.nmId));
    return request<ABTestCardsPage>(`/ab-tests/cards?${params.toString()}`, { signal });
  },
  getABTests: (connectionId?: number, status?: string) => {
    const params = new URLSearchParams();
    if (connectionId) params.set('connection_id', String(connectionId));
    if (status && status !== 'all') params.set('status', status);
    return request<{ items: ABTest[]; total: number }>(`/ab-tests${params.toString() ? `?${params.toString()}` : ''}`);
  },
  getABTest: (testId: number) => request<ABTest>(`/ab-tests/${testId}`),
  deleteABTest: (testId: number) => request<void>(`/ab-tests/${testId}`, { method: 'DELETE' }),
  createABTest: (payload: ABTestCreatePayload) => request<ABTest>('/ab-tests', { method: 'POST', body: JSON.stringify(payload) }),
  uploadABTestVariant: (testId: number, position: number, file: File) => {
    const body = new FormData();
    body.append('file', file);
    return request<ABTest>(`/ab-tests/${testId}/variants/${position}`, { method: 'POST', body });
  },
  previewABTestImage: (file: File) => {
    const body = new FormData();
    body.append('file', file);
    return request<{ image_url: string; file_name: string; width: number; height: number }>('/ab-tests/preview-image', { method: 'POST', body });
  },
  setABTestVariantSource: (testId: number, position: number, source_url: string) =>
    request<ABTest>(`/ab-tests/${testId}/variants/${position}/source`, { method: 'POST', body: JSON.stringify({ source_url }) }),
  deleteABTestVariant: (testId: number, position: number) => request<ABTest>(`/ab-tests/${testId}/variants/${position}`, { method: 'DELETE' }),
  startABTest: (testId: number, payload: { auto_deposit: boolean; deposit_rub?: number; funding_source?: 'auto' | 'account' | 'mutual' | 'bonus' }, idempotencyKey?: string) => request<ABTest>(`/ab-tests/${testId}/start`, { method: 'POST', headers: idempotencyKey ? { 'X-Idempotency-Key': idempotencyKey } : undefined, body: JSON.stringify(payload) }),
  stopABTest: (testId: number) => request<ABTest>(`/ab-tests/${testId}/stop`, { method: 'POST' }),
  syncABTest: (testId: number) => request<ABTest>(`/ab-tests/${testId}/sync`, { method: 'POST' }),
  reconcileABTest: (testId: number) => request<ABTest>(`/ab-tests/${testId}/reconcile`, { method: 'POST' }),
};
