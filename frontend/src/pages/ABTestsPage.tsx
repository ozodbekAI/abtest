import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react';
import { ArrowRight, Check, CheckCircle2, CircleAlert, Eye, FlaskConical, Gift, ImagePlus, LoaderCircle, Pause, Play, Plus, RefreshCw, Search, Settings2, Sparkles, Target, Trash2, Trophy, Upload, Wallet, X } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '../api/client';
import { AuthenticatedImage } from '../components/AuthenticatedImage';
import { AppShell } from '../components/AppShell';
import { StoreSwitcher } from '../components/StoreSwitcher';
import type { ABTest, ABTestCard, ABTestCardCursor, WBConnection, WBPromotionBalance } from '../types';
import { imageUrl } from '../utils/media';
import { publishActiveStoreId, readActiveStoreId, subscribeActiveStoreId } from '../utils/activeStore';

type Filter = 'all' | 'running' | 'draft' | 'finished' | 'failed';
type WizardStep = 1 | 2 | 3;
type TestImage =
  | { kind: 'upload'; file: File; previewUrl?: string }
  | { kind: 'card'; url: string; name: string }
  | { kind: 'main'; url: string; name: string };
type TestSlot = TestImage | null;
type FundingSource = 'account' | 'mutual' | 'bonus';
type StartConfirmation = {
  autoDeposit: boolean;
  cpm: number;
  requiredBudget: number;
  testedVariantCount: number;
  sourceLabel: string;
};
type MinimumCpmConfirmation = {
  testId: number;
  minimumCpm: number;
  recalculatedBudget: number;
  message: string;
};

const MAX_UPLOAD_BYTES = 32 * 1024 * 1024;
const MAX_TEST_IMAGES = 5;
const SUPPORTED_UPLOAD_TYPES = new Set(['image/jpeg', 'image/jpg', 'image/png', 'image/webp', 'image/bmp', 'image/gif', 'image/tiff', 'image/tif', 'image/x-tiff']);
const SUPPORTED_UPLOAD_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tif', '.tiff']);

async function isTiffFile(file: File) {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
  const contentType = file.type.toLowerCase();
  if (extension === '.tif' || extension === '.tiff' || contentType === 'image/tiff' || contentType === 'image/tif' || contentType === 'image/x-tiff') return true;
  const header = new Uint8Array(await file.slice(0, 4).arrayBuffer());
  return (header[0] === 0x49 && header[1] === 0x49 && header[2] === 0x2a && header[3] === 0x00)
    || (header[0] === 0x4d && header[1] === 0x4d && header[2] === 0x00 && header[3] === 0x2a);
}

async function validateUploadFile(file: File): Promise<string | null> {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
  const contentType = (file.type || '').toLowerCase();
  const tiffExtension = extension === '.tif' || extension === '.tiff';
  const typeAllowed = !contentType || SUPPORTED_UPLOAD_TYPES.has(contentType) || (tiffExtension && contentType === 'application/octet-stream');
  if (!SUPPORTED_UPLOAD_EXTENSIONS.has(extension) || !typeAllowed) {
    return 'Поддерживаются JPG, PNG, WEBP, BMP, GIF и TIFF.';
  }
  if (!file.size) return 'Файл изображения пуст';
  if (file.size > MAX_UPLOAD_BYTES) return 'Размер изображения не должен превышать 32 МБ';

  // Do not trust only the extension/MIME. A TIFF renamed to .jpeg would
  // otherwise create a 200 blob URL but still render as a broken image.
  const header = new Uint8Array(await file.slice(0, 12).arrayBuffer());
  const isJpeg = header[0] === 0xff && header[1] === 0xd8 && header[2] === 0xff;
  const isPng = header.slice(0, 8).every((value, index) => value === [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a][index]);
  const isGif = header[0] === 0x47 && header[1] === 0x49 && header[2] === 0x46 && header[3] === 0x38 && (header[4] === 0x37 || header[4] === 0x39) && header[5] === 0x61;
  const isBmp = header[0] === 0x42 && header[1] === 0x4d;
  const isWebp = header[0] === 0x52 && header[1] === 0x49 && header[2] === 0x46 && header[3] === 0x46 && header[8] === 0x57 && header[9] === 0x45 && header[10] === 0x42 && header[11] === 0x50;
  const isTiff = (header[0] === 0x49 && header[1] === 0x49 && header[2] === 0x2a && header[3] === 0x00)
    || (header[0] === 0x4d && header[1] === 0x4d && header[2] === 0x00 && header[3] === 0x2a);
  if (!isJpeg && !isPng && !isGif && !isBmp && !isWebp && !isTiff) {
    return 'Файл не является поддерживаемым изображением. Проверьте, что это не переименованный файл другого формата.';
  }

  // Chromium usually cannot decode TIFF in createImageBitmap. The server
  // converts it to JPEG and returns a preview instead.
  if (!isTiff && typeof createImageBitmap === 'function') {
    let bitmap: ImageBitmap | null = null;
    try {
      bitmap = await createImageBitmap(file);
      if (bitmap.width < 700 || bitmap.height < 900) return 'Минимальный размер изображения — 700 × 900 пикселей';
    } catch {
      return 'Не удалось прочитать изображение. Выберите корректный JPG, PNG, WEBP, BMP, GIF или TIFF.';
    } finally {
      bitmap?.close();
    }
  }
  return null;
}

function statusLabel(status: string, operationState?: string) {
  if (operationState === 'reconciliation_required') return 'Требуется сверка';
  return ({ running: 'Активный', draft: 'Черновик', finished: 'Завершён', failed: 'Требуется сверка', stopped: 'Остановлен' } as Record<string, string>)[status] || status;
}

function isIncident(test: ABTest) {
  return test.operation_state === 'reconciliation_required' || test.status === 'failed' || test.status === 'stopped';
}

function formatNumber(value: number) { return new Intl.NumberFormat('ru-RU').format(Math.round(value || 0)); }
function formatRub(value: number) { return `${new Intl.NumberFormat('ru-RU').format(Math.round(value || 0))} ₽`; }
const MIN_TEST_BUDGET_RUB = 1200;
function calculateRequiredBudget(photosCount: number, viewsPerVariant: number, cpmRub: number) {
  const rawSpendRub = Math.ceil((Math.max(0, photosCount) * Math.max(0, viewsPerVariant) * Math.max(0, cpmRub)) / 1000);
  return Math.ceil(Math.max(rawSpendRub, MIN_TEST_BUDGET_RUB) / 100) * 100;
}
function newOperationKey(testId: number) {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `start-${testId}-${Date.now()}`;
}

export function ABTestsPage() {
  const navigate = useNavigate();
  const [connections, setConnections] = useState<WBConnection[]>([]);
  const [activeId, setActiveId] = useState<number | null>(() => readActiveStoreId());
  const [tests, setTests] = useState<ABTest[]>([]);
  const [filter, setFilter] = useState<Filter>('all');
  const [loading, setLoading] = useState(true);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [testsError, setTestsError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const activeConnection = useMemo(() => connections.find((item) => item.id === activeId) || connections[0] || null, [activeId, connections]);

  const loadConnections = async () => {
    try {
      const result = await api.getWBConnections();
      setConnectionsError(null);
      setConnections(result);
      const selected = result.find((item) => item.id === activeId) || result[0];
      if (selected) { setActiveId(selected.id); publishActiveStoreId(selected.id); }
    } catch (error: any) {
      const message = error?.message || 'Не удалось загрузить магазины';
      setConnectionsError(message);
      toast.error(message);
    }
  };

  const loadTests = async () => {
    if (!activeConnection) { setTests([]); setLoading(false); return; }
    setLoading(true);
    try {
      setTests((await api.getABTests(activeConnection.id)).items);
      setTestsError(null);
    }
    catch (error: any) {
      const message = error?.message || 'Не удалось загрузить A/B-тесты';
      setTestsError(message);
      toast.error(message);
    }
    finally { setLoading(false); }
  };

  useEffect(() => { void loadConnections(); }, []);
  useEffect(() => { if (activeConnection) void loadTests(); }, [activeConnection?.id]);
  useEffect(() => subscribeActiveStoreId((id) => {
    if (id === activeId) return;
    setActiveId(id);
    // A wizard keeps card/photos from one store in memory. It must never
    // survive a store change made in another tab.
    setCreateOpen(false);
    setTests([]);
  }), [activeId]);

  const visibleTests = tests.filter((test) => filter === 'all' || test.status === filter || (filter === 'failed' && isIncident(test)));
  const setStore = (id: number) => { setCreateOpen(false); setActiveId(id); publishActiveStoreId(id); };

  return <AppShell>
    <div className="page-wrap ab-tests-page">
      <div className="page-heading ab-heading"><div><div className="eyebrow eyebrow-dark"><FlaskConical size={14} /> ЭКСПЕРИМЕНТЫ</div><h1>A/B-тесты изображений</h1><p>Сравнивайте креативы на реальном трафике Wildberries.</p></div><div className="page-heading-actions">{activeConnection && <StoreSwitcher connections={connections} activeConnection={activeConnection} onSelect={setStore} />}<button className="primary-button" onClick={() => setCreateOpen(true)} disabled={!activeConnection?.ready_for_ab_tests}><Plus size={17} /> Создать тест</button></div></div>
      {!activeConnection && connectionsError && <EmptyState title="Не удалось загрузить магазины" text={connectionsError} action={<button className="primary-button" onClick={() => void loadConnections()}><RefreshCw size={16} /> Повторить</button>} />}
      {!activeConnection && !connectionsError && <EmptyState title="Сначала подключите магазин" text="Для создания эксперимента нужен токен WB с доступом к «Контенту» и «Продвижению»." action={<Link to="/settings" className="primary-button">Открыть настройки <ArrowRight size={16} /></Link>} />}
      {activeConnection && !activeConnection.ready_for_ab_tests && <div className="ab-access-warning"><CircleAlert size={19} /><div><strong>Магазин не готов к A/B-тестам</strong><span>Проверьте доступ токена к категориям «Контент» и «Продвижение».</span></div><Link to="/settings" className="outline-button compact">Проверить доступ</Link></div>}
      {activeConnection && <><div className="ab-overview-grid"><OverviewCard icon={<FlaskConical />} label="Всего тестов" value={tests.length.toString()} /><OverviewCard icon={<Play />} label="Активные" value={tests.filter((test) => test.status === 'running').length.toString()} accent="green" /><OverviewCard icon={<Trophy />} label="Завершены" value={tests.filter((test) => test.status === 'finished').length.toString()} accent="violet" /><OverviewCard icon={<Target />} label="Показы собрано" value={formatNumber(tests.reduce((sum, test) => sum + test.total_views, 0))} /></div><div className="ab-list-toolbar"><div><h2>Ваши эксперименты</h2><p>Каждый тест работает только с выбранным магазином.</p></div><div className="ab-tabs">{([['all', 'Все'], ['running', 'Активные'], ['draft', 'Черновики'], ['finished', 'Завершённые'], ['failed', 'Ошибки']] as [Filter, string][]).map(([value, label]) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{label}<span>{value === 'all' ? tests.length : tests.filter((test) => value === 'failed' ? isIncident(test) : test.status === value).length}</span></button>)}</div></div>{testsError && <div className="ab-error-banner reconciliation"><CircleAlert size={18} /><div><strong>Не удалось обновить список тестов</strong><span>{testsError}</span></div><button className="outline-button compact" onClick={() => void loadTests()}><RefreshCw size={15} /> Повторить</button></div>}{loading ? <div className="ab-loading"><LoaderCircle className="spin" size={23} /> Загружаем эксперименты…</div> : visibleTests.length ? <div className="ab-test-list">{visibleTests.map((test) => <TestRow key={test.id} test={test} onOpen={() => navigate(`/ab-tests/${test.id}`)} />)}</div> : <EmptyState title={filter === 'all' ? 'Экспериментов пока нет' : 'В этом разделе пусто'} text="Создайте тест из двух и более изображений, чтобы начать сравнение." action={<button className="primary-button" onClick={() => setCreateOpen(true)}><Plus size={16} /> Создать первый тест</button>} />}</>}
    </div>
    {createOpen && <CreateTestModal connection={activeConnection} onClose={() => setCreateOpen(false)} onCreated={(test) => { setCreateOpen(false); navigate(`/ab-tests/${test.id}`); }} />}
  </AppShell>;
}

function OverviewCard({ icon, label, value, accent = '' }: { icon: React.ReactNode; label: string; value: string; accent?: string }) {
  return <div className={`ab-overview-card ${accent}`}><span className="ab-overview-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong></div></div>;
}

function TestRow({ test, onOpen }: { test: ABTest; onOpen: () => void }) {
  const preview = test.variants.find((variant) => variant.position === (test.winner_variant_order || test.current_variant_order))?.image_url || test.variants.find((variant) => variant.position > 0)?.image_url;
  return <button className="ab-test-row" onClick={onOpen}><div className="ab-test-preview">{preview ? <AuthenticatedImage src={imageUrl(preview, `test-${test.id}`)} alt="" /> : <ImagePlus size={22} />}</div><div className="ab-test-info"><div className="ab-test-title"><strong>{test.title}</strong><span className={`ab-status ${test.status}`}>{test.status === 'running' && <i />}{statusLabel(test.status, test.operation_state)}</span></div><p>Артикул {test.nm_id} · Магазин «{test.store_name}» · {test.variants.filter((variant) => variant.source_type !== 'control').length} вариантов</p></div><div className="ab-test-stat"><small>Показы</small><strong>{formatNumber(test.total_views)}</strong></div><div className="ab-test-stat"><small>CTR</small><strong>{test.total_views ? `${((test.total_clicks / test.total_views) * 100).toFixed(2)}%` : '—'}</strong></div><div className="ab-test-action"><ArrowRight size={18} /></div></button>;
}

function EmptyState({ title, text, action }: { title: string; text: string; action: React.ReactNode }) {
  return <div className="ab-empty"><span><Sparkles size={20} /></span><h3>{title}</h3><p>{text}</p>{action}</div>;
}

function CreateTestModal({ connection, onClose, onCreated }: { connection: WBConnection | null; onClose: () => void; onCreated: (test: ABTest) => void }) {
  const [step, setStep] = useState<WizardStep>(1);
  const [search, setSearch] = useState('');
  const [cards, setCards] = useState<ABTestCard[]>([]);
  const [nextCursor, setNextCursor] = useState<ABTestCardCursor | null>(null);
  const [nextLoading, setNextLoading] = useState(false);
  const [card, setCard] = useState<ABTestCard | null>(null);
  const [photoPickerIndex, setPhotoPickerIndex] = useState<number | null>(null);
  const [files, setFiles] = useState<TestSlot[]>([null, null, null, null]);
  const [title, setTitle] = useState('');
  const [views, setViews] = useState(1000);
  const [cpm, setCpm] = useState(300);
  const placement = 'combined' as const;
  const [skipCurrent, setSkipCurrent] = useState(false);
  const [keepWinner, setKeepWinner] = useState(true);
  const [autoDeposit, setAutoDeposit] = useState(true);
  const [fundingSource, setFundingSource] = useState<FundingSource>('account');
  const [promotionBalance, setPromotionBalance] = useState<WBPromotionBalance | null>(null);
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [balanceError, setBalanceError] = useState<string | null>(null);
  const [deleteTestMedia, setDeleteTestMedia] = useState(true);
  const [busy, setBusy] = useState(false);
  const [validatingIndex, setValidatingIndex] = useState<number | null>(null);
  const [cardLoading, setCardLoading] = useState(false);
  const [startConfirmation, setStartConfirmation] = useState<StartConfirmation | null>(null);
  const [minimumCpmConfirmation, setMinimumCpmConfirmation] = useState<MinimumCpmConfirmation | null>(null);
  const objectUrls = useRef(new Map<File, string>());
  const objectUrlCleanupTimer = useRef<number | null>(null);
  const imageCacheScope = card ? `card-${card.nm_id}` : 'card-picker';
  const previews = useMemo(() => files.map((item) => {
    if (!item) return null;
    if (item.kind !== 'upload') return imageUrl(item.url, imageCacheScope);
    if (item.previewUrl) return item.previewUrl;
    const cached = objectUrls.current.get(item.file);
    if (cached) return cached;
    const url = URL.createObjectURL(item.file);
    objectUrls.current.set(item.file, url);
    return url;
  }), [files, imageCacheScope]);
  const selectedImages = files.filter((item): item is TestImage => Boolean(item));
  const testImages = selectedImages.filter((item) => item.kind !== 'main');
  const selectedCardUrls = new Set(selectedImages.filter((item) => item.kind === 'card' || item.kind === 'main').map((item) => item.url));
  const testedVariantCount = testImages.length + (skipCurrent ? 0 : 1);
  const comparisonReady = testedVariantCount >= 2 && testedVariantCount <= MAX_TEST_IMAGES;
  const safeViews = Number.isFinite(views) && views > 0 ? views : 0;
  const safeCpm = Number.isFinite(cpm) && cpm > 0 ? cpm : 0;
  const estimatedBudget = calculateRequiredBudget(testedVariantCount, safeViews, safeCpm);
  // The campaign budget is derived from the actual test stages. There is no
  // second manual value that could disagree with CPM × impressions.
  const budget = estimatedBudget;
  const requiredBudget = estimatedBudget;
  const stepNames = ['Карточка', 'Изображения и опции', 'Параметры'];
  const selectedFundingBalance = fundingSource === 'account'
    ? promotionBalance?.account_balance || 0
    : fundingSource === 'mutual'
      ? promotionBalance?.mutual_balance || 0
      : promotionBalance?.promo_bonus_balance || 0;
  const fundingReady = !autoDeposit || Boolean(promotionBalance && !balanceLoading && selectedFundingBalance >= requiredBudget);
  const canProceed = step === 1 ? Boolean(card) : step === 2 ? comparisonReady : fundingReady;
  const hasSearched = search.trim().length > 0;

  const loadPromotionBalance = async () => {
    if (!connection?.id) {
      setPromotionBalance(null);
      return;
    }
    setBalanceLoading(true);
    setBalanceError(null);
    try {
      setPromotionBalance(await api.getWBPromotionBalance(connection.id));
    } catch (error: any) {
      setPromotionBalance(null);
      setBalanceError(error?.message || 'Не удалось получить баланс продвижения');
    } finally {
      setBalanceLoading(false);
    }
  };

  useEffect(() => {
    if (step === 3) void loadPromotionBalance();
  }, [step, connection?.id]);

  useEffect(() => {
    const activeFiles = new Set(files.flatMap((item) => item?.kind === 'upload' ? [item.file] : []));
    objectUrls.current.forEach((url, file) => {
      if (!activeFiles.has(file)) {
        URL.revokeObjectURL(url);
        objectUrls.current.delete(file);
      }
    });
  }, [files]);

  useEffect(() => {
    // React StrictMode performs a development-only setup/cleanup/setup cycle.
    // Delay final cleanup by one task so that cycle cannot revoke live previews.
    if (objectUrlCleanupTimer.current !== null) {
      window.clearTimeout(objectUrlCleanupTimer.current);
      objectUrlCleanupTimer.current = null;
    }
    return () => {
      objectUrlCleanupTimer.current = window.setTimeout(() => {
        objectUrls.current.forEach((url) => URL.revokeObjectURL(url));
        objectUrls.current.clear();
        objectUrlCleanupTimer.current = null;
      }, 0);
    };
  }, []);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (photoPickerIndex !== null) setPhotoPickerIndex(null);
      else onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [onClose, photoPickerIndex]);

  useEffect(() => {
    // Keep the file picker compatible with TIFF as well. The JSX for the
    // compact slot grid is intentionally kept on one render expression, so
    // update the generated input attributes when the wizard is mounted.
    document.querySelectorAll<HTMLInputElement>('.ab-wizard-modal input[type="file"]').forEach((input) => {
      input.accept = 'image/jpeg,image/png,image/webp,image/bmp,image/gif,image/tiff,image/x-tiff,.tif,.tiff';
    });
    const parameterPanel = document.querySelector<HTMLElement>('.ab-wizard-modal .wizard-params-step');
    const numericInputs = parameterPanel?.querySelectorAll<HTMLInputElement>('input[type="number"]');
    const calculatedBudgetInput = numericInputs?.[2];
    if (calculatedBudgetInput) {
      calculatedBudgetInput.readOnly = true;
      calculatedBudgetInput.setAttribute('aria-label', 'Минимальный бюджет рассчитан автоматически');
    }
  }, [files.length, step]);

  useEffect(() => {
    if (!connection || !connection.ready_for_ab_tests || card) return;
    let active = true;
    const controller = new AbortController();
    setCards([]);
    setNextCursor(null);
    const timer = window.setTimeout(() => {
      setCardLoading(true);
      const loadPages = async () => {
        // Load only the first page while typing. The next page is fetched
        // explicitly by the user to keep the modal responsive on large catalogs.
        const result = await api.getABTestCards(connection.id, search, controller.signal);
        if (!active) return;
        setCards(result.items);
        setNextCursor(result.next_cursor || null);
      };
      void loadPages().catch((error: any) => { if (active && error?.name !== 'AbortError') toast.error(error?.message || 'Не удалось загрузить карточки'); })
        .finally(() => { if (active) setCardLoading(false); });
    }, 280);
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [connection?.id, search, connection?.ready_for_ab_tests, card]);

  const loadNextCards = async () => {
    if (!connection || !nextCursor || nextLoading) return;
    setNextLoading(true);
    try {
      const result = await api.getABTestCards(connection.id, search, undefined, nextCursor);
      setCards((current) => [...current, ...result.items]);
      setNextCursor(result.next_cursor || null);
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось загрузить следующие карточки');
    } finally {
      setNextLoading(false);
    }
  };

  const chooseCard = (value: ABTestCard) => {
    setCard(value);
    setTitle(value.title ? `Тест: ${value.title}` : `A/B-тест ${value.nm_id}`);
    setFiles([value.main_photo_url ? { kind: 'main', url: value.main_photo_url, name: 'Главное фото' } : null, null, null, null]);
    setCards([]);
    setStep(2);
  };

  const changeCard = () => {
    setCard(null);
    setFiles([null, null, null, null]);
    setSearch('');
    setStep(1);
  };

  const setSkipCurrentOption = (checked: boolean) => {
    setSkipCurrent(checked);
    if (!card?.main_photo_url) return;
    setFiles((current) => {
      if (checked) {
        return current.map((item) => item?.kind === 'main' || (item?.kind === 'card' && item.url === card.main_photo_url) ? null : item);
      }
      const main = { kind: 'main' as const, url: card.main_photo_url as string, name: 'Главное фото' };
      const alternatives = current.filter((item) => item && item.kind !== 'main');
      const next = [main, ...alternatives];
      if (next.length > current.length) {
        toast.message('Контрольное фото возвращено в первый слот. Последний вариант убран из-за лимита слотов.');
      }
      return Array.from({ length: current.length }, (_, index) => next[index] || null);
    });
  };

  const setFile = async (index: number, event: ChangeEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const file = event.target.files?.[0];
    if (!file) return;
    setValidatingIndex(index);
    try {
      const error = await validateUploadFile(file);
      if (error) {
        setFiles((current) => current.map((item, position) => position === index ? null : item));
        toast.error(error);
        return;
      }
      let previewUrl: string | undefined;
      const tiff = await isTiffFile(file);
      if (tiff) {
        const preview = await api.previewABTestImage(file);
        previewUrl = preview.image_url;
      }
      setFiles((current) => current.map((item, position) => position === index ? { kind: 'upload', file, previewUrl } : item));
      toast.success(tiff ? 'TIFF конвертирован в JPEG для предпросмотра.' : 'Фото добавлено в слот. Оно загрузится при запуске теста.');
    } finally {
      input.value = '';
      setValidatingIndex((current) => current === index ? null : current);
    }
  };

  const openPhotoPicker = (index: number) => setPhotoPickerIndex(index);

  const addCardPhoto = (url: string, index: number) => {
    if (photoPickerIndex === null || selectedCardUrls.has(url) || (skipCurrent && url === card?.main_photo_url)) return;
    setFiles((current) => current.map((item, position) => position === photoPickerIndex ? { kind: 'card', url, name: `Фото карточки ${index + 1}` } : item));
    setPhotoPickerIndex(null);
  };

  const removeSlot = (index: number) => {
    if (files.length <= 2) return;
    setFiles((current) => current.filter((_, position) => position !== index));
  };

  const goNext = () => {
    if (step === 1 && !card) { toast.error('Сначала выберите карточку товара'); return; }
    if (step === 2 && !comparisonReady) {
      toast.error(testedVariantCount > MAX_TEST_IMAGES ? `В одном тесте можно сравнивать максимум ${MAX_TEST_IMAGES} изображения` : skipCurrent ? 'Добавьте минимум два изображения для сравнения' : 'Добавьте ещё одно изображение: главное фото участвует в тесте');
      return;
    }
    setStep((current) => Math.min(3, current + 1) as WizardStep);
  };

  const goBack = () => setStep((current) => Math.max(1, current - 1) as WizardStep);

  const openCreatedDraft = async (testId: number) => {
    setMinimumCpmConfirmation(null);
    try {
      onCreated(await api.getABTest(testId));
    } catch {
      onClose();
    }
  };

  const retryAfterCpmUpdate = async () => {
    if (!minimumCpmConfirmation) return;
    const pending = minimumCpmConfirmation;
    setMinimumCpmConfirmation(null);
    setBusy(true);
    try {
      const latest = await api.getABTest(pending.testId);
      const retried = await api.startABTest(latest.id, { auto_deposit: autoDeposit, funding_source: autoDeposit ? fundingSource : 'auto' }, newOperationKey(latest.id));
      toast.success('CPM обновлён, A/B-тест запущен');
      onCreated(retried);
    } catch (error: any) {
      toast.error(error?.message || 'Не удалось повторить запуск после пересчёта CPM');
      await openCreatedDraft(pending.testId);
    }
    finally { setBusy(false); }
  };

  const startTest = async () => {
    if (!connection || !card || !startConfirmation) return;
    setStartConfirmation(null);
    setBusy(true);
    let createdTest: ABTest | null = null;
    try {
      let test = await api.createABTest({ connection_id: connection.id, nm_id: card.nm_id, title: title.trim() || `A/B-тест ${card.nm_id}`, skip_current_photo: skipCurrent, keep_winner_as_main: keepWinner, delete_test_media: deleteTestMedia, views_per_variant: views, cpm_rub: cpm, budget_rub: estimatedBudget, placement });
      createdTest = test;
      for (let index = 0; index < testImages.length; index += 1) {
        const image = testImages[index];
        test = image.kind === 'upload'
          ? await api.uploadABTestVariant(test.id, index + 1, image.file)
          : await api.setABTestVariantSource(test.id, index + 1, image.url);
        createdTest = test;
      }
      test = await api.startABTest(test.id, { auto_deposit: autoDeposit, funding_source: autoDeposit ? fundingSource : 'auto' }, newOperationKey(test.id));
      toast.success('A/B-тест запущен');
      onCreated(test);
    } catch (error: any) {
      const detail = error?.details?.detail;
      if (detail?.code === 'minimum_cpm_increased' && createdTest) {
        const minimumCpm = Number(detail.minimum_cpm);
        const recalculatedBudget = Number(detail.recalculated_budget);
        const safeMinimumCpm = Number.isFinite(minimumCpm) ? minimumCpm : cpm;
        const safeRecalculatedBudget = Number.isFinite(recalculatedBudget) ? recalculatedBudget : requiredBudget;
        setCpm(safeMinimumCpm);
        setMinimumCpmConfirmation({
          testId: createdTest.id,
          minimumCpm: safeMinimumCpm,
          recalculatedBudget: safeRecalculatedBudget,
          message: detail.message || 'Минимальная ставка Wildberries изменилась.',
        });
        return;
      }
      toast.error(error?.message || 'Не удалось запустить A/B-тест');
      // The draft and its durable incident state remain available after a
      // failed external step. Open it immediately so the user can reconcile
      // instead of being trapped in a modal with no recovery path.
      if (createdTest) {
        try {
          onCreated(await api.getABTest(createdTest.id));
        } catch {
          onCreated(createdTest);
        }
      }
    }
    finally { setBusy(false); }
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (step < 3) { goNext(); return; }
    if (!connection || !card) { toast.error('Выберите карточку товара'); return; }
    if (!comparisonReady) { toast.error(testedVariantCount > MAX_TEST_IMAGES ? `В одном тесте можно сравнивать максимум ${MAX_TEST_IMAGES} изображения` : skipCurrent ? 'Добавьте минимум два изображения для сравнения' : 'Добавьте ещё одно изображение: главное фото участвует в тесте'); setStep(2); return; }
    if (views < 300) { toast.error('Для надёжного результата укажите минимум 300 показов на вариант'); return; }
    if (autoDeposit && !fundingReady) {
      toast.error(balanceError || `В выбранном источнике недостаточно средств для бюджета ${requiredBudget.toLocaleString('ru-RU')} ₽`);
      return;
    }
    setStartConfirmation({
      autoDeposit,
      cpm,
      requiredBudget,
      testedVariantCount,
      sourceLabel: fundingSourceLabel(fundingSource),
    });
  };


  const renderParametersStep = () => <section className="wizard-panel wizard-params-step"><div className="wizard-panel-heading"><span className="wizard-number">03</span><div><h3>Параметры и запуск</h3><p>Укажите название теста, объём данных и бюджет продвижения.</p></div></div><div className="wizard-fields"><label className="ab-field wizard-field-wide">Название теста<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Например, Весенний креатив" required /></label><div className="ab-fields-two"><label className="ab-field">Показы на вариант<input type="number" min={300} max={1000000} value={views} onChange={(event) => setViews(Number(event.target.value))} /><small>Минимум 300 показов</small></label><label className="ab-field">CPM, ₽<input type="number" min={1} value={cpm} onChange={(event) => setCpm(Number(event.target.value))} /><small>Ставка за 1 000 показов</small></label></div><label className="ab-field">Минимальный бюджет кампании, ₽<input type="number" min={1} value={budget} readOnly aria-label="Минимальный бюджет рассчитан автоматически" /><small>CPM × показы × этапы; минимум 1 200 ₽, округление вверх до 100 ₽</small></label><div className="wizard-unified-note"><span>Тип ставки</span><strong>Единая ставка · поиск и рекомендации</strong></div></div><FundingSourcePanel balance={promotionBalance} loading={balanceLoading} error={balanceError} source={fundingSource} onSourceChange={setFundingSource} onRefresh={() => void loadPromotionBalance()} autoDeposit={autoDeposit} requiredBudget={requiredBudget} /><div className="wizard-budget-card"><div><span>Расчёт запуска</span><strong>{requiredBudget.toLocaleString('ru-RU')} ₽</strong></div><div><small>{testedVariantCount} этапа · {views.toLocaleString('ru-RU')} показов на каждый</small><small>При CPM {cpm.toLocaleString('ru-RU')} ₽ · {autoDeposit ? 'автопополнение включено' : 'автопополнение отключено'}</small></div></div><div className="wizard-final-note"><ShieldIcon /><span>Перед запуском сервис ещё раз проверит доступ токена к «Контенту» и «Продвижению».</span></div></section>;
  const renderStep = () => {
    if (step === 3) return renderParametersStep();
    if (step === 1) return <section className="wizard-panel wizard-card-step"><div className="wizard-panel-heading"><span className="wizard-number">01</span><div><h3>Выберите карточку</h3><p>Найдите товар, для которого хотите сравнить изображения.</p></div></div><div className="card-search-box"><Search size={17} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по артикулу или названию" autoFocus />{cardLoading && <LoaderCircle className="spin" size={16} />}</div>{cards.length > 0 && <div className="card-search-results wizard-card-results">{cards.slice(0, 8).map((item) => <button type="button" key={item.nm_id} onClick={() => chooseCard(item)}><span>{item.main_photo_url ? <img src={imageUrl(item.main_photo_url)} alt="" /> : <ImagePlus size={18} />}</span><div><strong>{item.title || 'Без названия'}</strong><small>Артикул {item.nm_id} · {item.vendor_code || 'Артикул продавца не указан'}</small></div><ArrowRight size={15} /></button>)}{nextCursor && <button type="button" className="card-load-more" onClick={() => void loadNextCards()} disabled={nextLoading}>{nextLoading ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />} Показать ещё карточки</button>}</div>}{!cardLoading && cards.length === 0 && <div className="wizard-empty-hint"><span><Search size={16} /></span><div><strong>{hasSearched ? 'Карточка не найдена' : 'Начните с артикула или названия'}</strong><small>{hasSearched ? 'Проверьте nmID и убедитесь, что карточка существует в выбранном магазине Wildberries.' : 'Мы загрузим актуальные данные товара и все изображения из карточки Wildberries.'}</small></div></div>}</section>;

    if (step === 2) return <><section className="wizard-panel wizard-images-step"><div className="wizard-panel-heading"><span className="wizard-number">02</span><div><h3>Добавьте изображения и опции</h3><p>В каждом слоте выберите фото из карточки или загрузите новый креатив.</p></div><b className="wizard-counter">{testImages.length} вариантов</b></div><div className="wizard-selected-card"><div className="selected-card-image">{card?.main_photo_url && <img src={imageUrl(card.main_photo_url)} alt="" />}</div><div><strong>{card?.title || 'Без названия'}</strong><small>Артикул {card?.nm_id} · {card?.photos.length || 0} фото в карточке</small></div><button type="button" onClick={changeCard}>Изменить</button></div><div className="variant-grid wizard-variant-grid">{files.map((item, index) => <div className={`variant-slot ${item ? 'filled' : ''} ${item?.kind === 'main' ? 'main-slot' : ''}`} key={`${index}-${item?.kind === 'upload' ? item.file.name : item?.kind === 'card' || item?.kind === 'main' ? item.url : 'empty'}`}><div className="variant-slot-head"><span>{item?.kind === 'main' ? 'Главное фото' : `Вариант ${index + 1}`}</span>{files.length > 2 && item?.kind !== 'main' && <button type="button" onClick={() => removeSlot(index)} aria-label="Удалить слот"><Trash2 size={14} /></button>}</div>{item ? <><div className="variant-file-preview"><img src={previews[index] || ''} alt="" />{item.kind !== 'main' && <button type="button" onClick={() => setFiles((current) => current.map((source, position) => position === index ? null : source))} aria-label="Убрать изображение"><X size={14} /></button>}</div><small className="variant-file-name">{item.kind === 'upload' ? item.file.name : item.name}</small><em className={`variant-source-badge ${item.kind === 'main' ? 'control' : ''}`}>{item.kind === 'upload' ? 'Файл' : item.kind === 'main' ? 'Контроль' : 'Карточка'}</em></> : <div className="variant-empty-actions"><button type="button" onClick={() => openPhotoPicker(index)}><ImagePlus size={16} /><span>Из карточки</span></button><label><Upload size={16} /><span>{validatingIndex === index ? 'Проверяем…' : 'С компьютера'}</span><input type="file" accept="image/jpeg,image/png,image/webp,image/bmp,image/gif,image/tiff,.tif,.tiff" disabled={validatingIndex === index} onChange={(event) => void setFile(index, event)} /></label></div>}</div>)}{files.length < MAX_TEST_IMAGES && <button type="button" className="variant-add-slot" onClick={() => setFiles((current) => [...current, null])}><Plus size={18} /><span>Добавить слот</span><small>{files.length}/{MAX_TEST_IMAGES}</small></button>}</div><div className="wizard-inline-note"><span>i</span>Выберите от 2 до 5 вариантов. Первое фото карточки добавлено как контрольное и не загружается повторно.</div><div className="wizard-image-options"><div className="wizard-options-heading"><div><strong>Правила эксперимента</strong><small>Настройте, как будет работать тест с главным изображением.</small></div><span>Опции</span></div><div className="wizard-option-list"><label className={skipCurrent ? 'checked' : ''}><input type="checkbox" checked={skipCurrent} onChange={(event) => setSkipCurrentOption(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Не тестировать главное фото</strong><small>Уберём его из первого слота и начнём тест с выбранных вариантов.</small></span><span className="wizard-option-value">Без контроля</span></label><label className={keepWinner ? 'checked' : ''}><input type="checkbox" checked={keepWinner} onChange={(event) => setKeepWinner(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Сделать победившее фото основным</strong><small>После завершения теста победитель автоматически станет главным фото карточки.</small></span></label><label className={autoDeposit ? 'checked' : ''}><input type="checkbox" checked={autoDeposit} onChange={(event) => setAutoDeposit(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Пополнить рекламную кампанию автоматически</strong><small>Если бюджета недостаточно, нужная сумма будет запрошена со счёта Wildberries.</small></span></label><label className={deleteTestMedia ? 'checked' : ''}><input type="checkbox" checked={deleteTestMedia} onChange={(event) => setDeleteTestMedia(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Удалить тестовые файлы после завершения</strong><small>Загруженные изображения будут очищены после окончания эксперимента.</small></span></label></div></div></section>{photoPickerIndex !== null && <div className="photo-picker-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setPhotoPickerIndex(null); }}><section className="photo-picker" role="dialog" aria-modal="true" aria-labelledby="photo-picker-title"><header><div><span className="modal-kicker">ФОТО КАРТОЧКИ</span><h3 id="photo-picker-title">Выберите изображение</h3><p>Оно будет добавлено в слот «Вариант {photoPickerIndex + 1}».</p></div><button type="button" className="icon-button" onClick={() => setPhotoPickerIndex(null)} aria-label="Закрыть"><X size={17} /></button></header><div className="photo-picker-grid">{card?.photos.map((url, index) => { const isSelected = selectedCardUrls.has(url) || (skipCurrent && url === card.main_photo_url); return <button type="button" key={`${url}-${index}`} className={isSelected ? 'selected' : ''} disabled={isSelected} onClick={() => addCardPhoto(url, index)}><img src={imageUrl(url) } alt={`Фото ${index + 1}`} /><span>Фото {index + 1}</span>{isSelected && <i><Check size={13} /></i>}</button>; })}</div></section></div>}</>;

    if (step === 3) return <section className="wizard-panel wizard-options-step"><div className="wizard-panel-heading"><span className="wizard-number">03</span><div><h3>Настройте правила теста</h3><p>Выберите, как сервис должен работать с карточкой и рекламной кампанией.</p></div></div><div className="wizard-option-list"><label className={skipCurrent ? 'checked' : ''}><input type="checkbox" checked={skipCurrent} onChange={(event) => setSkipCurrent(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Не начинать с текущего фото</strong><small>Сервис сразу покажет первый выбранный вариант, не тратя этап на контрольное изображение.</small></span><span className="wizard-option-value">Рекомендуется</span></label><label className={keepWinner ? 'checked' : ''}><input type="checkbox" checked={keepWinner} onChange={(event) => setKeepWinner(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Сделать победившее фото основным</strong><small>После завершения теста победитель автоматически станет главным изображением карточки.</small></span></label><label className={autoDeposit ? 'checked' : ''}><input type="checkbox" checked={autoDeposit} onChange={(event) => setAutoDeposit(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Пополнить рекламную кампанию автоматически</strong><small>Если бюджета будет недостаточно, нужная сумма будет запрошена со счёта Wildberries после подтверждения.</small></span></label><label className={deleteTestMedia ? 'checked' : ''}><input type="checkbox" checked={deleteTestMedia} onChange={(event) => setDeleteTestMedia(event.target.checked)} /><span className="wizard-option-check"><Check size={15} /></span><span className="wizard-option-copy"><strong>Удалить тестовые файлы после завершения</strong><small>Локальные загруженные изображения будут очищены после окончания эксперимента.</small></span></label></div><div className="wizard-summary-card"><div className="wizard-summary-icon"><ImagePlus size={18} /></div><div><strong>{card?.title || 'Выбранная карточка'}</strong><span>Артикул {card?.nm_id} · {selectedImages.length} вариантов изображения</span></div><b>{skipCurrent ? 'Без контроля' : 'С контролем'}</b></div></section>;

    return <section className="wizard-panel wizard-params-step"><div className="wizard-panel-heading"><span className="wizard-number">04</span><div><h3>Параметры и запуск</h3><p>Укажите название теста, объём данных и бюджет продвижения.</p></div></div><div className="wizard-fields"><label className="ab-field wizard-field-wide">Название теста<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Например, Весенний креатив" required /></label><div className="ab-fields-two"><label className="ab-field">Показы на вариант<input type="number" min={300} max={1000000} value={views} onChange={(event) => setViews(Number(event.target.value))} /><small>Минимум 300 показов</small></label><label className="ab-field">CPM, ₽<input type="number" min={1} value={cpm} onChange={(event) => setCpm(Number(event.target.value))} /><small>Ставка за 1 000 показов</small></label></div><label className="ab-field">Минимальный бюджет кампании, ₽<input type="number" min={1} value={budget} readOnly aria-label="Минимальный бюджет рассчитан автоматически" /><small>CPM × показы × этапы; минимум 1 200 ₽, округление вверх до 100 ₽</small></label><div className="wizard-unified-note"><span>Тип ставки</span><strong>Единая ставка · поиск и рекомендации</strong></div></div><div className="wizard-budget-card"><div><span>Расчёт запуска</span><strong>{requiredBudget.toLocaleString('ru-RU')} ₽</strong></div><div><small>{testedVariantCount} этапа · {views.toLocaleString('ru-RU')} показов на каждый</small><small>При CPM {cpm.toLocaleString('ru-RU')} ₽ · {autoDeposit ? 'автопополнение включено' : 'автопополнение отключено'}</small></div></div><div className="wizard-final-note"><ShieldIcon /><span>Перед запуском сервис ещё раз проверит доступ токена к «Контенту» и «Продвижению».</span></div></section>;
  };

  if (startConfirmation) {
    return <StartConfirmationModal confirmation={startConfirmation} busy={busy} onClose={() => setStartConfirmation(null)} onConfirm={() => void startTest()} />;
  }
  if (minimumCpmConfirmation) {
    return <MinimumCpmModal confirmation={minimumCpmConfirmation} busy={busy} onClose={() => void openCreatedDraft(minimumCpmConfirmation.testId)} onConfirm={() => void retryAfterCpmUpdate()} />;
  }

  return <div className="ab-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="ab-create-modal ab-wizard-modal" role="dialog" aria-modal="true" aria-labelledby="ab-create-title"><header className="ab-modal-header"><div className="ab-modal-header-copy"><span className="modal-kicker">НОВЫЙ ЭКСПЕРИМЕНТ</span><h2 id="ab-create-title">Создать A/B-тест</h2><p>Пройдите три шага — сервис по очереди проверит выбранные изображения.</p></div><button className="icon-button" type="button" onClick={onClose} aria-label="Закрыть"><X size={18} /></button><nav className="wizard-stepper" aria-label="Шаги создания теста">{stepNames.map((name, index) => { const value = (index + 1) as WizardStep; return <button type="button" key={name} className={`${value === step ? 'active' : ''} ${value < step ? 'done' : ''}`} onClick={() => value < step && setStep(value)} disabled={value > step}><span>{value < step ? <Check size={13} /> : `0${value}`}</span><strong>{name}</strong></button>; })}</nav></header><form onSubmit={submit}><div className="ab-wizard-body">{renderStep()}</div><footer className="ab-modal-footer"><span><ShieldIcon /> Доступ WB проверяется перед запуском</span><div><button type="button" className="outline-button" onClick={onClose}>Отмена</button>{step > 1 && <button type="button" className="outline-button wizard-back-button" onClick={goBack} disabled={busy}>Назад</button>}<button type="submit" className="primary-button" disabled={busy || !connection?.ready_for_ab_tests || !canProceed}>{busy ? <><LoaderCircle className="spin" size={16} /> Запускаем…</> : step < 3 ? <>Далее <ArrowRight size={16} /></> : <><Play size={16} /> Создать и запустить</>}</button></div></footer></form></section></div>;
}

function StartConfirmationModal({
  confirmation,
  busy,
  onClose,
  onConfirm,
}: {
  confirmation: StartConfirmation;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return <div className="ab-confirm-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section className="ab-confirm-modal" role="dialog" aria-modal="true" aria-labelledby="start-confirm-title">
      <button type="button" className="icon-button ab-confirm-close" onClick={onClose} disabled={busy} aria-label="Закрыть"><X size={17} /></button>
      <div className="ab-confirm-icon"><Wallet size={21} /></div>
      <span className="modal-kicker">ПРОВЕРКА ЗАПУСКА</span>
      <h2 id="start-confirm-title">Проверьте параметры запуска</h2>
      <p>Перед созданием кампании проверьте ставку и рассчитанный бюджет. После подтверждения сервис выполнит запуск в Wildberries.</p>
      <div className="ab-confirm-summary">
        <div><small>CPM</small><strong>{formatRub(confirmation.cpm)}</strong><span>за 1 000 показов</span></div>
        <div><small>Необходимый бюджет</small><strong>{formatRub(confirmation.requiredBudget)}</strong><span>{confirmation.testedVariantCount} этапа теста</span></div>
      </div>
      <div className="ab-confirm-source"><Wallet size={16} /><div><strong>Источник пополнения</strong><span>{confirmation.autoDeposit ? confirmation.sourceLabel : 'Пополнение вручную в кабинете WB'}</span></div><CheckCircle2 size={17} /></div>
      <div className="ab-confirm-note"><CircleAlert size={16} /><span>{confirmation.autoDeposit ? `Если средств в кампании будет недостаточно, Wildberries пополнит её на недостающую сумму через API. Новая кампания не создаётся повторно.` : 'Кампания должна быть пополнена вручную перед запуском. Автоматическое списание отключено.'}</span></div>
      <div className="ab-confirm-actions"><button type="button" className="outline-button" onClick={onClose} disabled={busy}>Отмена</button><button type="button" className="primary-button" onClick={onConfirm} disabled={busy}>{busy ? <><LoaderCircle className="spin" size={16} /> Запускаем…</> : <><Play size={16} /> {confirmation.autoDeposit ? 'Разрешить и запустить' : 'Подтвердить запуск'}</>}</button></div>
    </section>
  </div>;
}

function MinimumCpmModal({
  confirmation,
  busy,
  onClose,
  onConfirm,
}: {
  confirmation: MinimumCpmConfirmation;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return <div className="ab-confirm-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section className="ab-confirm-modal ab-cpm-confirm-modal" role="dialog" aria-modal="true" aria-labelledby="cpm-confirm-title">
      <button type="button" className="icon-button ab-confirm-close" onClick={onClose} disabled={busy} aria-label="Закрыть"><X size={17} /></button>
      <div className="ab-confirm-icon warning"><RefreshCw size={21} /></div>
      <span className="modal-kicker">ПАРАМЕТРЫ ОБНОВЛЕНЫ WB</span>
      <h2 id="cpm-confirm-title">Минимальная ставка изменилась</h2>
      <p>{confirmation.message}</p>
      <div className="ab-confirm-summary">
        <div><small>Новый CPM</small><strong>{formatRub(confirmation.minimumCpm)}</strong><span>минимально допустимая ставка</span></div>
        <div><small>Новый бюджет</small><strong>{formatRub(confirmation.recalculatedBudget)}</strong><span>рассчитан с учётом этапов</span></div>
      </div>
      <div className="ab-confirm-note"><CircleAlert size={16} /><span>Сервис обновил параметры черновика. Повторный запуск использует уже созданный тест и не создаёт дубликат.</span></div>
      <div className="ab-confirm-actions"><button type="button" className="outline-button" onClick={onClose} disabled={busy}>Открыть черновик</button><button type="button" className="primary-button" onClick={onConfirm} disabled={busy}>{busy ? <><LoaderCircle className="spin" size={16} /> Запускаем…</> : <><RefreshCw size={16} /> Пересчитать и повторить</>}</button></div>
    </section>
  </div>;
}

function ShieldIcon() { return <span className="ab-shield">✓</span>; }

function fundingSourceLabel(source: FundingSource) {
  return source === 'account' ? 'счёт Продвижения' : source === 'mutual' ? 'баланс взаиморасчётов' : 'промо-бонусы WB';
}

function FundingSourcePanel({
  balance,
  loading,
  error,
  source,
  onSourceChange,
  onRefresh,
  autoDeposit,
  requiredBudget,
}: {
  balance: WBPromotionBalance | null;
  loading: boolean;
  error: string | null;
  source: FundingSource;
  onSourceChange: (source: FundingSource) => void;
  onRefresh: () => void;
  autoDeposit: boolean;
  requiredBudget: number;
}) {
  const amount = source === 'account'
    ? balance?.account_balance || 0
    : source === 'mutual'
      ? balance?.mutual_balance || 0
      : balance?.promo_bonus_balance || 0;
  const options: Array<{ value: FundingSource; title: string; description: string }> = [
    { value: 'account', title: 'Счёт Продвижения', description: 'Пополнение из рекламного счёта WB' },
    { value: 'mutual', title: 'Баланс взаиморасчётов', description: 'Списание из доступного баланса' },
    { value: 'bonus', title: 'Промо-бонусы WB', description: 'Использовать начисленные бонусы' },
  ];

  return <section className="wizard-funding-panel" aria-labelledby="funding-source-title">
    <div className="wizard-funding-heading"><div><strong id="funding-source-title">Баланс и источник пополнения</strong><small>Выберите, откуда списать деньги, если бюджету кампании не хватит.</small></div><button type="button" className="wizard-refresh-balance" onClick={onRefresh} disabled={loading}><RefreshCw size={13} className={loading ? 'spin' : ''} /> Обновить</button></div>
    {error && <div className="wizard-funding-error"><CircleAlert size={14} /><span>{error}</span></div>}
    <div className="wizard-funding-options" role="radiogroup" aria-label="Источник пополнения">
      {options.map((option) => {
        const value = option.value === 'account' ? balance?.account_balance || 0 : option.value === 'mutual' ? balance?.mutual_balance || 0 : balance?.promo_bonus_balance || 0;
        const selected = source === option.value;
        return <button type="button" key={option.value} className={`wizard-funding-option ${selected ? 'selected' : ''} ${balance && value < requiredBudget && autoDeposit ? 'insufficient' : ''}`} onClick={() => onSourceChange(option.value)} role="radio" aria-checked={selected}>
          <span className="wizard-funding-icon">{option.value === 'bonus' ? <Gift size={16} /> : <Wallet size={16} />}</span>
          <span className="wizard-funding-copy"><strong>{option.title}</strong><small>{option.description}</small></span>
          <span className="wizard-funding-amount">{loading && !balance ? '…' : `${formatNumber(value)} ₽`}</span>
          <span className="wizard-funding-radio" aria-hidden="true">{selected && <i />}</span>
        </button>;
      })}
    </div>
    <div className="wizard-funding-status"><span>Выбрано: <strong>{fundingSourceLabel(source)}</strong></span><b className={amount >= requiredBudget ? 'enough' : 'not-enough'}>{loading && !balance ? 'Проверяем баланс…' : amount >= requiredBudget ? 'Достаточно для запуска' : `Не хватает ${formatNumber(requiredBudget - amount)} ₽`}</b></div>
    {balance?.cashbacks?.length ? <small className="wizard-cashback-note">Также доступны промо-кэшбэки: {formatNumber(balance.cashbacks.reduce((sum, item) => sum + (item.sum || 0), 0))} ₽. Они применяются WB по своим ограничениям.</small> : null}
  </section>;
}
