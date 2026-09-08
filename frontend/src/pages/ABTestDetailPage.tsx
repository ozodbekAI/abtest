import { useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, CircleAlert, Clock3, Eye, LoaderCircle, Pause, Play, RefreshCw, Trophy } from 'lucide-react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '../api/client';
import { AuthenticatedImage } from '../components/AuthenticatedImage';
import { AppShell } from '../components/AppShell';
import type { ABTest, ABTestVariant } from '../types';
import { imageUrl } from '../utils/media';

function formatNumber(value: number) { return new Intl.NumberFormat('ru-RU').format(Math.round(value || 0)); }
function formatRub(value: number) { return `${new Intl.NumberFormat('ru-RU').format(Math.round(value || 0))} ₽`; }
function formatOptionalRub(value?: number | null) { return value == null ? '—' : formatRub(value); }
function formatDecimalRub(value?: number | null) { return value == null ? '—' : `${new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 2 }).format(value)} ₽`; }
function calculateCpc(spend: number, clicks: number) { return clicks > 0 ? spend / clicks : null; }
function calculateCr(orders: number, clicks: number) { return clicks > 0 ? (orders / clicks) * 100 : null; }
function calculateCpm(spend: number, views: number) { return views > 0 ? (spend / views) * 1000 : null; }
function formatPercent(value: number) { return `${Number(value || 0).toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 2 })}%`; }
const MIN_TEST_BUDGET_RUB = 1200;
function calculateRequiredBudget(photosCount: number, viewsPerVariant: number, cpmRub: number) {
  const rawSpendRub = Math.ceil((Math.max(0, photosCount) * Math.max(0, viewsPerVariant) * Math.max(0, cpmRub)) / 1000);
  return Math.ceil(Math.max(rawSpendRub, MIN_TEST_BUDGET_RUB) / 100) * 100;
}
function newOperationKey(testId: number) {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `resume-${testId}-${Date.now()}`;
}
function statusLabel(status: string, operationState?: string) { if (operationState === 'reconciliation_required') return 'Требуется сверка'; return ({ running: 'Активный', draft: 'Черновик', finished: 'Завершён', failed: 'Сверка', stopped: 'Остановлен' } as Record<string, string>)[status] || status; }
function decisionLabel(decision?: string | null) { return ({ winner_found: 'Победитель определён', no_clear_winner: 'Явного победителя нет', insufficient_data: 'Недостаточно данных', statistics_not_attributable: 'Нельзя надёжно распределить статистику по фото', test_interrupted: 'Тест остановлен до завершения' } as Record<string, string>)[decision || ''] || 'Победитель определяется'; }

export function ABTestDetailPage() {
  const { testId } = useParams();
  const navigate = useNavigate();
  const id = Number(testId);
  const [test, setTest] = useState<ABTest | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [resumeConfirmOpen, setResumeConfirmOpen] = useState(false);

  const load = async (manual = false) => {
    if (!Number.isInteger(id)) return;
    if (!manual) setLoading(true);
    try {
      setTest(await api.getABTest(id));
      setLoadError(null);
    }
    catch (error: any) {
      const message = error?.message || 'Не удалось загрузить тест';
      setLoadError(message);
      toast.error(message);
    }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [id]);
  useEffect(() => { if (test?.status !== 'running') return; const timer = window.setInterval(() => void load(true), 30000); return () => window.clearInterval(timer); }, [test?.status, id]);

  const action = async (kind: 'sync' | 'stop' | 'reconcile' | 'resume') => {
    setBusy(true);
    try {
      const result = kind === 'sync'
        ? await api.syncABTest(id)
        : kind === 'stop'
          ? await api.stopABTest(id)
          : kind === 'resume'
            ? await api.startABTest(id, { auto_deposit: true }, newOperationKey(id))
            : await api.reconcileABTest(id);
      setTest(result);
      if (kind === 'resume') setResumeConfirmOpen(false);
      toast.success(
        kind === 'sync'
          ? 'Статистика обновлена'
          : kind === 'stop'
              ? 'Тест остановлен и состояние проверено'
              : kind === 'resume'
                ? 'Существующая кампания продолжена'
                : result.status === 'running'
                  ? 'Тест продолжен в существующей кампании'
                  : 'Операция сверена'
      );
    } catch (error: any) {
      const detail = error?.details?.detail;
      if (kind === 'resume' && detail?.code === 'minimum_cpm_increased') {
        toast.error(detail.message || 'Минимальная ставка Wildberries изменилась. Требуется новое подтверждение.');
        await load(true);
        setResumeConfirmOpen(true);
      } else {
        toast.error(error?.message || 'Не удалось выполнить действие');
        await load(true);
      }
    }
    finally { setBusy(false); }
  };

  if (loading && !test) return <AppShell><div className="page-wrap"><div className="ab-loading"><LoaderCircle className="spin" size={23} /> Загружаем тест…</div></div></AppShell>;
  if (!test) return <AppShell><div className="page-wrap"><div className="ab-empty"><h3>{loadError ? 'Не удалось загрузить тест' : 'Тест не найден'}</h3><p>{loadError || 'Проверьте ссылку и выбранный магазин.'}</p><div className="ab-empty-actions">{loadError && <button className="primary-button" onClick={() => void load()} disabled={loading}><RefreshCw size={16} /> Повторить</button>}<Link to="/ab-tests" className="outline-button">Вернуться к тестам</Link></div></div></div></AppShell>;

  const current = test.variants.find((variant) => variant.position === test.current_variant_order);
  const winner = test.variants.find((variant) => variant.position === test.winner_variant_order) || null;
  const progress = test.views_per_variant ? Math.min(100, ((current?.views || 0) / test.views_per_variant) * 100) : 0;
  const needsReconciliation = test.operation_state === 'reconciliation_required' || test.status === 'failed';
  const canResumeExistingCampaign = test.status === 'draft' && Boolean(test.wb_campaign_id) && ['created', 'stopped'].includes(test.campaign_state) && !test.started_at;
  const resumeVariantCount = test.variants.filter((variant) => variant.source_type !== 'control').length + (test.skip_current_photo ? 0 : 1);
  const resumeCalculatedBudget = calculateRequiredBudget(resumeVariantCount, test.views_per_variant, test.cpm_rub);
  const resumeBudget = Math.max(Number(test.budget_rub || 0), resumeCalculatedBudget);
  // The backend is the source of truth. Do not infer a winner from aggregate
  // campaign totals: WB fullstats does not attribute clicks to a photo.
  const displayWinner = winner;
  const totalPhotoCount = test.variants.length;
  const completedPhotoCount = test.status === 'finished'
    ? totalPhotoCount
    : Math.min(totalPhotoCount, Math.max(0, (test.current_variant_order || 1) - 1));
  const progressPercent = test.status === 'finished' ? 100 : progress;

  return <AppShell><div className="page-wrap ab-detail-page">
    <DetailPageHeader test={test} busy={busy} needsReconciliation={needsReconciliation} canResumeExistingCampaign={canResumeExistingCampaign} onBack={() => navigate('/ab-tests')} onSync={() => void action('sync')} onReconcile={() => void action('reconcile')} onResume={() => setResumeConfirmOpen(true)} onStop={() => void action('stop')} />
    {test.last_error && <div className={`ab-error-banner ${needsReconciliation ? 'reconciliation' : ''}`}><CircleAlert size={18} /><div><strong>{needsReconciliation ? 'Требуется сверка операции' : 'Информация о состоянии'}</strong><span>{test.last_error}{test.incident_id && <> · Инцидент: {test.incident_id}</>}</span></div></div>}
    <section className="ab-period-card"><div className="ab-period-heading"><span>Период проведения теста:</span><strong>{completedPhotoCount} из {totalPhotoCount} фото</strong></div><div className="ab-period-track"><i style={{ width: `${progressPercent}%` }} /></div>{test.status === 'running' && <small>После набора лимита сервис автоматически переключит фото и продолжит эксперимент.</small>}</section>
    <section className="ab-results-shell"><h2>Итоги теста</h2><FinalResultsSection test={test} variants={test.variants} winner={displayWinner} />
    </section>
    <section className="ab-settings-strip"><div><SettingsIcon /><span><strong>Настройки эксперимента</strong><small>{formatNumber(test.views_per_variant)} показов на вариант · CPM {formatRub(test.cpm_rub)} · {test.bid_type === 'unified' ? 'Единая ставка · поиск и рекомендации' : test.placement === 'search' ? 'Поиск' : 'Рекомендации'}</small></span></div><div className="ab-setting-flags">{test.skip_current_photo && <span><CheckCircle2 size={14} /> Без текущего фото</span>}{test.keep_winner_as_main && <span><Trophy size={14} /> Победитель отмечается отдельно</span>}</div></section>
  </div>{resumeConfirmOpen && <div className="ab-modal-backdrop ab-resume-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setResumeConfirmOpen(false); }}><section className="ab-resume-modal" role="dialog" aria-modal="true" aria-labelledby="resume-confirm-title"><button type="button" className="icon-button ab-resume-close" onClick={() => setResumeConfirmOpen(false)} aria-label="Закрыть">×</button><div className="ab-resume-icon"><Play size={20} /></div><span className="modal-kicker">ПОВТОРНЫЙ ЗАПУСК</span><h2 id="resume-confirm-title">Подтвердите запуск</h2><p>Будет продолжена существующая рекламная кампания Wildberries. Новая кампания создаваться не будет. Если бюджета не хватит, он будет пополнен автоматически на недостающую сумму.</p><div className="ab-resume-summary"><div><span>Кампания WB</span><strong>#{test.wb_campaign_id}</strong></div><div><span>Ставка CPM</span><strong>{formatRub(test.cpm_rub)}</strong></div><div><span>Необходимый бюджет</span><strong>{formatRub(resumeBudget)}</strong></div><div><span>Этапов теста</span><strong>{resumeVariantCount} · {formatNumber(test.views_per_variant)} показов</strong></div></div><div className="ab-resume-note"><CircleAlert size={16} /><span>После подтверждения сервис проверит баланс кампании, при необходимости выполнит пополнение через API и только затем отправит запуск.</span></div><div className="ab-resume-actions"><button type="button" className="outline-button" onClick={() => setResumeConfirmOpen(false)} disabled={busy}>Отмена</button><button type="button" className="primary-button" onClick={() => void action('resume')} disabled={busy}><Play size={16} /> Разрешить пополнение и запустить</button></div></section></div>}</AppShell>;
}

function SummaryMetric({ label, value }: { label: string; value: string }) { return <div className="ab-summary-metric"><small>{label}</small><strong>{value}</strong></div>; }
function DetailPageHeader({
  test,
  busy,
  needsReconciliation,
  canResumeExistingCampaign,
  onBack,
  onSync,
  onReconcile,
  onResume,
  onStop,
}: {
  test: ABTest;
  busy: boolean;
  needsReconciliation: boolean;
  canResumeExistingCampaign: boolean;
  onBack: () => void;
  onSync: () => void;
  onReconcile: () => void;
  onResume: () => void;
  onStop: () => void;
}) {
  const status = statusLabel(test.status, test.operation_state);
  const campaignCtr = test.total_views
    ? (Number.isFinite(test.total_ctr) ? test.total_ctr : (test.total_clicks / test.total_views) * 100)
    : 0;
  return <>
    <div className="ab-detail-toolbar">
      <button className="ab-detail-back-button" onClick={onBack}><ArrowLeft size={15} /> Назад</button>
      <div className="ab-detail-toolbar-right"><button className="outline-button ab-refresh-button" onClick={onSync} disabled={busy}><RefreshCw size={15} /> Обновить</button><div className="ab-detail-status"><strong>{formatRub(test.budget_rub)}</strong><span className={`ab-detail-status-pill ${test.status}`}>{test.status === 'finished' && <Trophy size={12} />}{status}</span></div></div>
    </div>
    <div className="ab-detail-intro"><h1>Подробности теста</h1><p>{test.title}</p></div>
    <section className="ab-summary-card">
      <div className="ab-summary-heading"><div><h2>{test.title}</h2><p>Артикул: {test.nm_id}{test.wb_campaign_id ? ` · Кампания ID: ${test.wb_campaign_id}` : ''}</p></div><div className="ab-summary-actions">{needsReconciliation && <button className="primary-button" onClick={onReconcile} disabled={busy}><RefreshCw size={15} /> Сверить операцию</button>}{canResumeExistingCampaign && <button className="outline-button" onClick={onResume} disabled={busy}><Play size={15} /> Продолжить запуск</button>}{test.status === 'running' && <button className="danger-outline-button" onClick={onStop} disabled={busy}><Pause size={15} /> Остановить</button>}</div></div>
      <div className="ab-summary-metrics"><SummaryMetric label="Кол-во фото" value={String(test.variants.length)} /><SummaryMetric label="Показов на одно фото" value={formatNumber(test.views_per_variant)} /><SummaryMetric label="Расход" value={formatRub(test.total_spend_rub)} /><SummaryMetric label="CTR по кампании" value={test.total_views ? formatPercent(campaignCtr) : '—'} /><SummaryMetric label="CPO кампании" value={formatOptionalRub(test.total_cpo)} /></div>
    </section>
  </>;
}
function FinalResultsSection({ test, variants, winner }: { test: ABTest; variants: ABTestVariant[]; winner: ABTestVariant | null }) {
  const ordered = [...variants].sort((left, right) => left.position - right.position);
  const isRunning = test.status === 'running';
  const isFinished = test.status === 'finished';
  const statsAreAggregate = test.stats_quality === 'aggregate_unverified';
  const totalCtr = test.total_views
    ? (Number.isFinite(test.total_ctr) ? test.total_ctr : (test.total_clicks / test.total_views) * 100)
    : 0;
  const bannerTitle = isFinished && winner
    ? 'Победитель найден'
    : isRunning
      ? 'Тест выполняется'
      : test.status === 'stopped'
        ? 'Тест остановлен'
        : test.status === 'failed'
          ? 'Требуется сверка'
          : 'Результаты теста';
  const bannerText = isFinished && winner
    ? 'Система определила лучший вариант по CTR и накопленной статистике.'
    : isRunning
      ? 'Варианты последовательно проверяются на трафике Wildberries.'
      : test.status === 'stopped' || test.status === 'failed'
        ? 'Результаты доступны ниже. После устранения причины тест можно продолжить.'
        : 'Статистика появится после запуска рекламной кампании.';
  const bannerNote = isFinished && winner
    ? 'Ниже показаны финальные результаты теста по всем фото.'
    : isRunning
      ? 'После завершения всех этапов система определит победителя.'
      : 'Итоговый победитель ещё не определён.';
  return <section className="ab-final-results">
    <div className={`ab-final-banner ${isFinished && winner ? 'complete' : 'pending'}`}><div><h2>{bannerTitle}</h2><p>{bannerText}</p><small>{bannerNote}</small></div></div>
    {statsAreAggregate && <div className="ab-inline-warning"><CircleAlert size={15} /> CTR кампании: <strong>{test.total_views ? `${totalCtr.toFixed(2)}%` : '—'}</strong> ({formatNumber(test.total_clicks)} кликов из {formatNumber(test.total_views)} показов). Для карточек показан приблизительный CTR текущего этапа: WB не передаёт достоверную разбивку по фото.</div>}
    {test.unallocated_views > 0 && <div className="ab-inline-warning"><CircleAlert size={15} /> Не распределено по фото: {formatNumber(test.unallocated_views)} показов, {formatNumber(test.unallocated_clicks)} кликов, {formatRub(test.unallocated_spend_rub)}.</div>}
    <div className="ab-final-cards">{ordered.map((variant) => {
      const isWinner = Boolean(winner && variant.id === winner.id);
      const isCurrent = isRunning && variant.position === test.current_variant_order;
      const badge = isWinner ? 'Лучший' : isCurrent ? 'Сейчас в тесте' : isFinished ? 'Нормально' : variant.views > 0 ? 'Собрано' : 'Ожидает';
      const ctr = variant.views ? `${statsAreAggregate ? '≈' : ''}${formatPercent(variant.ctr)}` : '—';
      const variantLabel = variant.position === 0 ? 'Главное фото' : `Фото ${variant.position}`;
      return <article className={`ab-final-card ${isWinner ? 'winner' : ''}`} key={variant.id}>
        <div className="ab-final-image">{variant.image_url ? <AuthenticatedImage src={imageUrl(variant.image_url)} alt={variantLabel} /> : <span><Clock3 size={24} /></span>}<b className={isCurrent ? 'current' : ''}>{badge}</b></div>
        <div className="ab-final-card-body"><strong>{variantLabel}</strong><em>{ctr}</em><div className="ab-final-card-stats"><span><Eye size={13} /> Показы: {formatNumber(variant.views)}</span><span>Клики: {formatNumber(variant.clicks)}</span></div></div>
      </article>;
    })}</div>
    <div className="ab-comparison-wrap"><table className="ab-comparison-table"><thead><tr><th>Показатель</th>{ordered.map((variant) => <th key={variant.id}>{variant.position === 0 ? 'Главное фото' : `Фото ${variant.position}`}</th>)}</tr></thead><tbody>
      <tr><th>Статус</th>{ordered.map((variant) => { const isWinner = Boolean(winner && variant.id === winner.id); const isCurrent = isRunning && variant.position === test.current_variant_order; const status = isWinner ? 'Лучший' : isCurrent ? 'В тесте' : isFinished ? 'Нормально' : variant.views > 0 ? 'Собрано' : 'Ожидает'; return <td key={variant.id}><span className={`ab-final-status ${isWinner ? 'winner' : isCurrent ? 'current' : ''}`}>{status}</span></td>; })}</tr>
      <tr><th>Показов</th>{ordered.map((variant) => <td key={variant.id}>{formatNumber(variant.views)}</td>)}</tr>
      <tr><th>Кликов</th>{ordered.map((variant) => <td key={variant.id}>{formatNumber(variant.clicks)}</td>)}</tr>
      <tr><th>CTR</th>{ordered.map((variant) => { const isWinner = Boolean(winner && variant.id === winner.id); return <td className={isWinner ? 'emphasis' : ''} key={variant.id}>{variant.views ? `${statsAreAggregate ? '≈' : ''}${formatPercent(variant.ctr)}` : '—'}</td>; })}</tr>
      <tr><th>В сравнении с лучшим CTR в тесте</th>{ordered.map((variant) => { const isWinner = Boolean(winner && variant.id === winner.id); return <td key={variant.id}>{winner ? (isWinner ? <strong className="ab-best-cell"><Trophy size={14} /> Лучший</strong> : `${relativeToWinner(variant, winner)}%`) : '—'}</td>; })}</tr>
    </tbody></table></div>
    <section className="ab-additional-metrics"><div className="ab-additional-heading"><div><h3>Дополнительные показатели</h3><p>Ключевые показатели эффективности рекламной кампании.</p></div><span>Данные WB</span></div><div className="ab-additional-grid"><div className="ab-additional-metric"><small>CPC кампании</small><strong>{formatDecimalRub(calculateCpc(test.total_spend_rub, test.total_clicks))}</strong><span>стоимость клика</span></div><div className="ab-additional-metric"><small>Заказы</small><strong>{formatNumber(test.total_orders)}</strong><span>по кампании</span></div><div className="ab-additional-metric"><small>CR кампании</small><strong>{formatPercent(calculateCr(test.total_orders, test.total_clicks) || 0)}</strong><span>заказы из кликов</span></div><div className="ab-additional-metric"><small>CPM факт</small><strong>{formatDecimalRub(calculateCpm(test.total_spend_rub, test.total_views))}</strong><span>за 1 000 показов</span></div></div></section>
  </section>;
}
function relativeToWinner(variant: ABTestVariant, winner: ABTestVariant) {
  if (!winner.ctr) return '—';
  const value = ((variant.ctr - winner.ctr) / winner.ctr) * 100;
  return `${value.toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}`;
}
function SettingsIcon() { return <span className="ab-settings-icon">⚙</span>; }
