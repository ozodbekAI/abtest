import { test, expect, type Page, type Route } from '@playwright/test';

const user = { id: 1, email: 'audit@example.invalid', first_name: 'Аудит', last_name: 'Проверка', is_active: true, is_verified: true, is_admin: false, created_at: '2026-09-27T10:00:00Z' };
const store = { id: 1, store_name: 'Тестовый магазин', status: 'ready', connected: true, ready_for_ab_tests: true, token_last4: 'xxxx', access: { content: true, promotion: true, analytics: true, statistics: true }, pings: {} };
const config = { minimum_budget_rub: 2000, budget_step_rub: 100, budget_guard_reserve_rub: 400, minimum_views_per_variant: 300, max_variants: 5 };
const variant = (position: number) => ({ id: position, position, source_type: position === 1 ? 'control' : 'card', image_url: null, views: 400, clicks: position === 1 ? 24 : 16, ctr: position === 1 ? 6 : 4, spend_rub: 100, is_winner: false, orders: 0, cpo: null });
const series = (overrides = {}) => ({ id: 1, connection_id: 1, store_name: store.store_name, nm_id: 1001, title: 'Проверка безопасности', status: 'running', wb_campaign_id: 777, bid_type: 'unified', skip_current_photo: false, keep_winner_as_main: false, delete_test_media: false, views_per_variant: 1000, cpm_rub: 300, budget_rub: 2400, placement: 'combined', current_variant_order: 1, winner_variant_order: null, winner_decision: null, operation_state: 'succeeded', campaign_state: 'running', media_status: 'variant_applied', stats_quality: 'aggregate_unverified', incident_id: null, unallocated_views: 800, unallocated_clicks: 40, unallocated_spend_rub: 200, funding_source: 'account', stage_views: 400, stage_clicks: 24, stage_spend_rub: 100, total_views: 800, total_clicks: 40, total_orders: 0, total_spend_rub: 200, total_ctr: 5, total_cpo: null, started_at: '2026-09-27T10:00:00Z', last_synced_at: '2026-09-27T10:05:00Z', created_at: '2026-09-27T10:00:00Z', draft_fingerprint: 'a'.repeat(64), start_confirmation_fingerprint: 'b'.repeat(64), stage_exposure_views: 400, variants: [variant(1), variant(2)], ...overrides });
const card = (id: number) => ({ nm_id: id, title: `Товар ${id}`, vendor_code: `v${id}`, main_photo_url: `https://images.example.invalid/${id}/1.jpg`, photos: [1, 2, 3].map((n) => `https://images.example.invalid/${id}/${n}.jpg`) });
const json = (route: Route, data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
type Mock = (route: Route, path: string) => Promise<boolean>;

async function setup(page: Page, current = series(), custom?: Mock, authenticated = true) {
  if (authenticated) await page.addInitScript(() => { localStorage.setItem('wb_optimizer_access_token', 'local-audit-marker'); localStorage.setItem('wb_optimizer_active_store_id', '1'); });
  await page.route('https://images.example.invalid/**', (route) => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="700" height="900"><rect width="700" height="900" fill="#abc"/></svg>' }));
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith('/api/')) return route.continue();
    if (custom && await custom(route, path)) return;
    if (path === '/api/auth/me') return json(route, user);
    if (path === '/api/wb/connections') return json(route, [store, { ...store, id: 2, store_name: 'Второй магазин' }]);
    if (path === '/api/ab-tests/config') return json(route, config);
    if (path === '/api/ab-tests/1') return json(route, current);
    if (path === '/api/ab-tests') return json(route, { items: [current], total: 1 });
    if (path === '/api/ab-tests/cards') return json(route, { items: [card(1001)], next_cursor: null });
    if (path === '/api/wb/promotion/balance') return json(route, { connection_id: 1, account_balance: 10000, mutual_balance: 10000, promo_bonus_balance: 0, cashbacks: [], fetched_at: '2026-09-27T10:00:00Z' });
    return json(route, { detail: `Unexpected local mock request: ${path}` }, 404);
  });
}

test('H01/H02: failed series retains stop, independent media state and continuing expense warning', async ({ page }) => {
  await setup(page, series({ status: 'failed', operation_state: 'reconciliation_required', campaign_state: 'unknown', media_status: 'restored', incident_id: 'INC-LOCAL-001', last_error: 'Ответ остановки потерян' }));
  await page.goto('/ab-tests/1');
  await expect(page.getByRole('button', { name: 'Остановить рекламу' })).toBeVisible();
  await expect(page.getByText('Остановка не подтверждена. Расход может продолжаться.')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Подтверждённое состояние' })).toContainText('Полный возврат оригинала подтверждён');
  await expect(page.getByRole('alert')).toContainText('Товар: 1001 · Серия: #1');
  await expect(page.getByRole('alert')).toContainText('INC-LOCAL-001');
  await page.screenshot({ path: '../audit/2026-09-27/frontend-incident.png', fullPage: true });
});

test('H01/F09: paused photo retry is not presented as running and aggregate photo CTR is hidden', async ({ page }) => {
  await setup(page, series({ campaign_state: 'paused', media_status: 'waiting_image_reupload' }));
  await page.goto('/ab-tests/1');
  await expect(page.getByText('Пауза: ожидается загрузка фото').first()).toBeVisible();
  await expect(page.getByText('Тест выполняется', { exact: true })).toHaveCount(0);
  await expect(page.locator('.ab-final-card em')).toHaveText(['—', '—']);
  await expect(page.getByText(/CTR отдельных фото скрыт/)).toBeVisible();
});

test('H01/H02: safe interruption does not promise continuation', async ({ page }) => {
  await setup(page, series({ status: 'stopped', campaign_state: 'stopped', media_status: 'restored', winner_decision: 'test_interrupted' }));
  await page.goto('/ab-tests/1');
  await expect(page.getByText('Безопасно прервано').first()).toBeVisible();
  await expect(page.getByText(/тест можно продолжить/)).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Продолжить запуск' })).toHaveCount(0);
});

test('C03/C07: minimum CPM continuation confirms exact snapshot with no new deposit', async ({ page }) => {
  let submitted: Record<string, unknown> | null = null;
  const paused = series({ campaign_state: 'paused', operation_state: 'awaiting_confirmation', minimum_cpm: 450 });
  await setup(page, paused, async (route, path) => {
    if (path !== '/api/ab-tests/1/start') return false;
    submitted = route.request().postDataJSON();
    await json(route, series({ cpm_rub: 450 }));
    return true;
  });
  await page.goto('/ab-tests/1');
  await page.getByRole('button', { name: 'Продолжить запуск' }).click();
  await expect(page.getByRole('dialog')).toContainText('450');
  await expect(page.getByRole('dialog')).toContainText('Собранные данные сохраняются');
  await page.getByRole('button', { name: 'Запустить без пополнения' }).click();
  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted).toMatchObject({ confirmed_cpm: 450, auto_deposit: false, funding_source: 'account', draft_fingerprint: paused.start_confirmation_fingerprint });
});

test('H01: successful stop response with unknown state is not announced as confirmed stop', async ({ page }) => {
  await setup(page, series(), async (route, path) => {
    if (path !== '/api/ab-tests/1/stop') return false;
    await json(route, series({ status: 'failed', operation_state: 'reconciliation_required', campaign_state: 'stop_requested', media_status: 'restored' }));
    return true;
  });
  await page.goto('/ab-tests/1');
  await page.getByRole('button', { name: 'Остановить рекламу' }).click();
  await expect(page.getByText('Остановка запрошена. Проверьте статус кампании и фото.')).toBeVisible();
  await expect(page.getByText('Остановка и состояние фото подтверждены', { exact: true })).toHaveCount(0);
});

test('B01: first catalog failure is not an empty catalog; retry recovers', async ({ page }) => {
  let fail = true;
  await setup(page, series(), async (route, path) => {
    if (path !== '/api/ab-tests/cards') return false;
    await json(route, fail ? { detail: 'Каталог временно недоступен' } : { items: [card(1001)], next_cursor: null }, fail ? 503 : 200);
    return true;
  });
  await page.goto('/ab-tests');
  await page.getByRole('button', { name: 'Создать тест', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Каталог получен не полностью');
  await expect(page.getByText('Карточка не найдена', { exact: true })).toHaveCount(0);
  fail = false;
  await page.getByRole('button', { name: 'Повторить', exact: true }).click();
  await expect(page.getByRole('button', { name: /Товар 1001/ })).toBeVisible();
});

test('B01: remaining cards in a complete first page can be revealed without next cursor', async ({ page }) => {
  await setup(page, series(), async (route, path) => {
    if (path !== '/api/ab-tests/cards') return false;
    await json(route, { items: Array.from({ length: 10 }, (_, i) => card(1001 + i)), next_cursor: null });
    return true;
  });
  await page.goto('/ab-tests');
  await page.getByRole('button', { name: 'Создать тест', exact: true }).click();
  await page.getByRole('button', { name: 'Показать ещё карточки' }).click();
  await expect(page.getByRole('button', { name: /Товар 1010/ })).toBeVisible();
});

test('B01: repeated cursor deduplicates products and labels incomplete list', async ({ page }) => {
  await setup(page, series(), async (route, path) => {
    if (path !== '/api/ab-tests/cards') return false;
    await json(route, { items: [card(1001)], next_cursor: { updatedAt: '2026-09-27', nmID: 1001 } });
    return true;
  });
  await page.goto('/ab-tests');
  await page.getByRole('button', { name: 'Создать тест', exact: true }).click();
  await page.getByRole('button', { name: 'Показать ещё карточки' }).click();
  await expect(page.getByRole('button', { name: /Товар 1001/ })).toHaveCount(1);
  await expect(page.getByRole('alert')).toContainText('WB повторил страницу каталога');
});

test('A06: store change from another tab closes the old-store draft', async ({ page, context }) => {
  await setup(page);
  await page.goto('/ab-tests');
  await page.getByRole('button', { name: 'Создать тест', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  const other = await context.newPage();
  await other.goto('/login');
  await other.evaluate(() => localStorage.setItem('wb_optimizer_active_store_id', '2'));
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('C04/C07: custom server budget config, source and exact photo set shown before start', async ({ page }) => {
  let created: Record<string, unknown> | null = null;
  let started: Record<string, unknown> | null = null;
  const draft = series({ status: 'draft', campaign_state: 'not_created', media_status: 'original', started_at: null, wb_campaign_id: null });
  await setup(page, series(), async (route, path) => {
    if (path === '/api/ab-tests' && route.request().method() === 'POST') { created = route.request().postDataJSON(); await json(route, draft); return true; }
    if (path === '/api/ab-tests/1/variants/1/source') { await json(route, draft); return true; }
    if (path === '/api/ab-tests/1/start') { started = route.request().postDataJSON(); await json(route, series()); return true; }
    return false;
  });
  await page.goto('/ab-tests');
  await page.getByRole('button', { name: 'Создать тест', exact: true }).click();
  await page.getByRole('button', { name: /Товар 1001/ }).click();
  await page.getByRole('button', { name: 'Из карточки', exact: true }).first().click();
  await page.getByRole('button', { name: 'Фото 2', exact: true }).click();
  await page.getByRole('button', { name: 'Далее', exact: true }).click();
  await page.getByRole('button', { name: 'Создать и запустить', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('Резерв: 400');
  await expect(page.getByRole('dialog')).toContainText('счёт Продвижения');
  await expect(page.getByRole('img', { name: /Подтверждаемое фото/ })).toHaveCount(2);
  await page.screenshot({ path: '../audit/2026-09-27/frontend-confirmation.png', fullPage: true });
  await page.getByRole('button', { name: 'Разрешить и запустить' }).click();
  await expect.poll(() => started).not.toBeNull();
  expect(created).toMatchObject({ budget_rub: 2400, cpm_rub: 300, views_per_variant: 1000 });
  expect(started).toMatchObject({ auto_deposit: true, deposit_rub: 2400, funding_source: 'account', draft_fingerprint: draft.start_confirmation_fingerprint });
});

test('H03: external HTML is text and never executed', async ({ page }) => {
  await setup(page, series({ title: '<img src=x onerror="window.auditXss=true">', last_error: '<script>window.auditXss=true</script>', status: 'failed', operation_state: 'reconciliation_required' }));
  await page.goto('/ab-tests/1');
  await expect(page.getByRole('alert')).toContainText('<script>window.auditXss=true</script>');
  expect(await page.evaluate(() => (window as unknown as { auditXss?: boolean }).auditXss)).toBeUndefined();
});

test('A09: registration conflict for verified account opens login, not email verification', async ({ page }) => {
  await setup(page, series(), async (route, path) => {
    if (path !== '/api/auth/register/start') return false;
    await json(route, { detail: 'Аккаунт с этой электронной почтой уже существует' }, 409);
    return true;
  }, false);
  await page.goto('/register');
  await page.getByLabel('Имя', { exact: true }).fill('Аудит');
  await page.getByLabel('Фамилия').fill('Проверка');
  await page.getByLabel('Электронная почта').fill('audit@example.invalid');
  await page.getByLabel('Пароль', { exact: true }).fill('test-password-123');
  await page.getByLabel('Подтвердите пароль', { exact: true }).fill('test-password-123');
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: 'Создать аккаунт' }).click();
  await expect(page).toHaveURL(/\/login$/);
});
