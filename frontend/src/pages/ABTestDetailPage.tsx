import { useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, CircleAlert, Clock3, Eye, LoaderCircle, Pause, Play, RefreshCw, Trophy } from 'lucide-react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '../api/client';
import { AuthenticatedImage } from '../components/AuthenticatedImage';
import { AppShell } from '../components/AppShell';
import type { ABTest, ABTestVariant } from '../types';
import { imageUrl } from '../utils/media';
import { campaignStateLabel, mediaStateLabel, statsStateLabel, testStatusLabel, decisionLabel, needsReconciliation as requiresReconciliation, canSafelyStop, safetyClosed, moduleActionLabel } from '../utils/abTestState';

function formatNumber(value: number) { return new Intl.NumberFormat('ru-RU').format(Math.round(value || 0)); }
function formatRub(value: number) { return `${new Intl.NumberFormat('ru-RU').format(Math.round(value || 0))} ₽`; }
function formatOptionalRub(value?: number | null) { return value == null ? '—' : formatRub(value); }
function formatDecimalRub(value?: number | null) { return value == null ? '—' : `${new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 2 }).format(value)} ₽`; }
function calculateCpc(spend: number, clicks: number) { return clicks > 0 ? spend / clicks : null; }
function calculateCr(orders: number, clicks: number) { return clicks > 0 ? (orders / clicks) * 100 : null; }
function calculateCpm(spend: number, views: number) { return views > 0 ? (spend / views) * 1000 : null; }
function formatPercent(value: number | null) { if (value == null) return '—'; return `${Number(value || 0).toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 2 })}%`; }
function newOperationKey(testId: number) {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `resume-${testId}-${Date.now()}`;
}
export function ABTestDetailPage() {
  const { testId } = useParams();
  const navigate = useNavigate();
  const id = Number(testId);
  const [test, setTest] = useState<ABTest | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [resumeConfirmation, setResumeConfirmation] = useState<ABTest | null>(null);

  const load = async (manual = false) => {
    if (!Number.isInteger(id) || id < 1) { setLoading(false); setLoadError('Некорректный номер серии'); return; }
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
  useEffect(() => { if (!test || (test.status !== 'running' && !requiresReconciliation(test))) return; const timer = window.setInterval(() => void load(true), 30000); return () => window.clearInterval(timer); }, [test?.status, test?.operation_state, test?.campaign_state, test?.media_status, id]);

  const action = async (kind: 'sync' | 'stop' | 'reconcile' | 'resume') => {
    if (busy || (kind === 'resume' && !resumeConfirmation)) return;
    setBusy(true);
    try {
      const result = kind === 'sync'
        ? await api.syncABTest(id)
        : kind === 'stop'
          ? await api.stopABTest(id)
          : kind === 'resume'
            ? await api.startABTest(id, { auto_deposit: false, confirmed_cpm: resumeConfirmation?.minimum_cpm || undefined, funding_source: resumeConfirmation?.funding_source as 'auto' | 'account' | 'mutual' | 'bonus', draft_fingerprint: resumeConfirmation?.start_confirmation_fingerprint || '' }, newOperationKey(id))
            : await api.reconcileABTest(id);
      setTest(result);
      if (kind === 'resume') setResumeConfirmation(null);
      toast.success(
        kind === 'sync'
          ? 'Статистика обновлена'
          : kind === 'stop'
              ? safetyClosed(result) ? 'Остановка и состояние фото подтверждены' : 'Остановка запрошена. Проверьте статус кампании и фото.'
              : kind === 'resume'
                ? result.campaign_state === 'running' ? 'Запуск существующей кампании подтверждён' : 'Запуск запрошен; проверяется состояние'
                : result.status === 'running'
                  ? 'Тест продолжен в существующей кампании'
                  : 'Операция сверена'
      );
    } catch (error: any) {
      const detail = error?.details?.detail;
      if (kind === 'resume' && detail?.code === 'minimum_cpm_increased') {
        toast.error(detail.message || 'Минимальная ставка Wildberries изменилась. Требуется новое подтверждение.');
        await load(true);
        setResumeConfirmation(null);
      } else {
        toast.error(error?.message || 'Не удалось выполнить действие');
        await load(true);
      }
    }
    finally { setBusy(false); }
  };

  if (loading && !test) return <AppShell><div className="page-wrap"><div className="ab-loading"><LoaderCircle className="spin" size={23} /> Загружаем тест…</div></div></AppShell>;
  if (!test) return <AppShell><div className="page-wrap"><div className="ab-empty"><h3>{loadError ? 'Не удалось загрузить тест' : 'Тест не найден'}</h3><p>{loadError || 'Проверьте ссылку и выбранный магазин.'}</p><div className="ab-empty-actions">{loadError && <button className="primary-button" onClick={() => void load()} disabled={loading}><RefreshCw size={16} /> Повторить</button>}<Link to="/ab-tests" className="outline-button">Вернуться к тестам</Link></div></div></div></AppShell>;

  const winner = test.variants.find((variant) => variant.position === test.winner_variant_order) || null;
  const progress = test.views_per_variant ? Math.min(100, ((test.stage_exposure_views || 0) / test.views_per_variant) * 100) : 0;
  const needsReconciliation = requiresReconciliation(test);
  const canResumeMinimumPause = test.status === 'running' && test.campaign_state === 'paused' && test.operation_state === 'awaiting_confirmation' && Boolean(test.minimum_cpm);
  const canResumeExistingCampaign = canResumeMinimumPause || test.status === 'draft' && test.operation_state !== 'reconciliation_required' && !test.incident_id && Boolean(test.wb_campaign_id) && ['created', 'paused'].includes(test.campaign_state) && !test.started_at;
  const resumeBudget = resumeConfirmation?.budget_rub || test.budget_rub;
  // The backend is the source of truth. Do not infer a winner from aggregate
  // campaign totals: WB fullstats does not attribute clicks to a photo.
  const displayWinner = test.stats_quality === 'stage_attributed' && test.winner_decision === 'winner_found' ? winner : null;
  const totalPhotoCount = test.variants.length;
  const completedPhotoCount = test.status === 'finished'
    ? totalPhotoCount
    : Math.min(totalPhotoCount, Math.max(0, (test.current_variant_order || 1) - 1));
  const progressPercent = test.status === 'finished' ? 100 : progress;

  return <AppShell><div className="page-wrap ab-detail-page">
    <DetailPageHeader test={test} busy={busy} needsReconciliation={needsReconciliation} canResumeExistingCampaign={canResumeExistingCampaign} onBack={() => navigate('/ab-tests')} onSync={() => void action('sync')} onReconcile={() => void action('reconcile')} onResume={() => setResumeConfirmation(test)} onStop={() => void action('stop')} />
    {loadError && <div className="ab-error-banner" role="alert"><CircleAlert size={18} /><div><strong>Данные не удалось обновить</strong><span>{loadError}. Ниже показаны последние полученные данные; текущее состояние WB может отличаться.</span></div></div>}
    {(test.last_error || needsReconciliation) && <div className="ab-error-banner reconciliation" role="alert"><CircleAlert size={18} /><div><strong>{testStatusLabel(test)}</strong><span>{test.last_error || 'Состояние кампании или фото требует проверки.'}</span><div className="ab-incident-help"><span>Магазин: {test.store_name} · Товар: {test.nm_id} · Серия: #{test.id} · Кампания WB: {test.wb_campaign_id ? `#${test.wb_campaign_id}` : 'не создана'}</span><span>Последний учтённый расход: {formatDecimalRub(test.total_spend_rub)} · Данные на: {test.last_synced_at ? new Date(test.last_synced_at).toLocaleString('ru-RU') : 'время не подтверждено'}</span><span>Кампания: {campaignStateLabel(test.campaign_state)} · Фото: {mediaStateLabel(test.media_status)}</span>{!['stopped', 'not_created', 'paused'].includes(test.campaign_state) && <b>Остановка не подтверждена. Расход может продолжаться.</b>}<b>Что делает модуль</b><span>{moduleActionLabel(test)}</span><b>Что делать</b><span>Откройте кампанию #{test.wb_campaign_id || '—'} в указанном кабинете WB. Если реклама работает, остановите её вручную или кнопкой «Остановить рекламу». Проверьте полный порядок фото; затем нажмите «Сверить операцию». Не повторяйте пополнение при неизвестном результате.</span><span>Инцидент: {test.incident_id || 'номер ещё не получен'}</span></div></div></div>}
    <section className="ab-state-grid" aria-label="Подтверждённое состояние"><div><small>Исход серии</small><strong>{testStatusLabel(test)}</strong></div><div><small>Кампания WB</small><strong>{campaignStateLabel(test.campaign_state)}</strong></div><div><small>Медиа</small><strong>{mediaStateLabel(test.media_status)}</strong></div><div><small>Статистика</small><strong>{statsStateLabel(test.stats_quality)}</strong></div><div><small>Последняя синхронизация</small><strong>{test.last_synced_at ? new Date(test.last_synced_at).toLocaleString('ru-RU') : 'Не подтверждена'}</strong></div><div><small>Остаток в кампании WB</small><strong>Не подтверждён</strong><small>Проверьте в кабинете WB; бюджет минус расход не доказывает остаток.</small></div></section>
    <section className="ab-period-card"><div className="ab-period-heading"><span>Период проведения теста:</span><strong>{completedPhotoCount} из {totalPhotoCount} фото</strong></div><div className="ab-period-track"><i style={{ width: `${progressPercent}%` }} /></div>{test.status === 'running' && <small>{moduleActionLabel(test)}</small>}</section>
    <section className="ab-results-shell"><h2>Итоги теста</h2>{test.stats_quality !== 'stage_attributed' && test.status === 'running' && <div className="ab-stats-note">Разбивка по фото пока не подтверждена. Если данные WB не позволят её проверить, победитель не будет выбран.</div>}<FinalResultsSection test={test} variants={test.variants} winner={displayWinner} />
    </section>
    <section className="ab-settings-strip"><div><SettingsIcon /><span><strong>Настройки эксперимента</strong><small>{formatNumber(test.views_per_variant)} показов на вариант · CPM {formatRub(test.cpm_rub)} · {test.bid_type === 'unified' ? 'Единая ставка · поиск и рекомендации' : test.placement === 'search' ? 'Поиск' : 'Рекомендации'}</small></span></div><div className="ab-setting-flags">{test.skip_current_photo && <span><CheckCircle2 size={14} /> Без текущего фото</span>}{test.keep_winner_as_main && <span><Trophy size={14} /> Победитель отмечается отдельно</span>}</div></section>
  </div>{resumeConfirmation && <div className="ab-modal-backdrop ab-resume-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setResumeConfirmation(null); }}><section className="ab-resume-modal" role="dialog" aria-modal="true" aria-labelledby="resume-confirm-title"><button type="button" className="icon-button ab-resume-close" disabled={busy} onClick={() => setResumeConfirmation(null)} aria-label="Закрыть">×</button><span className="modal-kicker">СУЩЕСТВУЮЩАЯ КАМПАНИЯ</span><h2 id="resume-confirm-title">Подтвердите запуск</h2>{resumeConfirmation.minimum_cpm && <p>Минимальный CPM WB вырос с {formatRub(resumeConfirmation.cpm_rub)} до {formatRub(resumeConfirmation.minimum_cpm)}. Подтверждается новая ставка при прежнем денежном пределе. Собранные данные сохраняются.</p>}<p>Запуск кампании #{resumeConfirmation.wb_campaign_id} на её существующем бюджете. Дополнительное пополнение не разрешается; при нехватке средств запуск будет заблокирован.</p><div className="ab-resume-summary"><div><span>Магазин / товар / серия</span><strong>{resumeConfirmation.store_name} / {resumeConfirmation.nm_id} / #{resumeConfirmation.id}</strong></div><div><span>CPM</span><strong>{formatRub(resumeConfirmation.minimum_cpm || resumeConfirmation.cpm_rub)}</strong></div><div><span>Подтверждаемый предел</span><strong>{formatRub(resumeBudget)}</strong></div><div><span>Дополнительное пополнение</span><strong>0 ₽</strong></div><div><span>Фото / показы на вариант</span><strong>{resumeConfirmation.variants.length} / {formatNumber(resumeConfirmation.views_per_variant)}</strong></div></div><div className="ab-resume-note"><CircleAlert size={16} /><span>Сервер проверит актуальность именно этих параметров и остаток WB. При изменении условий потребуется новое подтверждение.</span></div><div className="ab-resume-actions"><button type="button" className="outline-button" onClick={() => setResumeConfirmation(null)} disabled={busy}>Отмена</button><button type="button" className="primary-button" onClick={() => void action('resume')} disabled={busy}><Play size={16} /> Запустить без пополнения</button></div></section></div>}</AppShell>;

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
  const status = testStatusLabel(test);
  const campaignCtr = test.total_views
    ? (test.total_ctr != null && Number.isFinite(test.total_ctr) ? test.total_ctr : (test.total_clicks / test.total_views) * 100)
    : 0;
  return <>
    <div className="ab-detail-toolbar">
      <button className="ab-detail-back-button" onClick={onBack}><ArrowLeft size={15} /> Назад</button>
      <div className="ab-detail-toolbar-right"><button className="outline-button ab-refresh-button" onClick={onSync} disabled={busy}><RefreshCw size={15} /> Обновить</button><div className="ab-detail-status"><strong>{formatRub(test.budget_rub)}</strong><span className={`ab-detail-status-pill ${test.status}`}>{test.status === 'finished' && <Trophy size={12} />}{status}</span></div></div>
    </div>
    <div className="ab-detail-intro"><h1>Подробности теста</h1><p>{test.title}</p></div>
    <section className="ab-summary-card">
      <div className="ab-summary-heading"><div><h2>{test.title}</h2><p>Артикул: {test.nm_id}{test.wb_campaign_id ? ` · Кампания ID: ${test.wb_campaign_id}` : ''}</p></div><div className="ab-summary-actions">{needsReconciliation && <button className="primary-button" onClick={onReconcile} disabled={busy}><RefreshCw size={15} /> Сверить операцию</button>}{canResumeExistingCampaign && <button className="outline-button" onClick={onResume} disabled={busy}><Play size={15} /> Продолжить запуск</button>}{canSafelyStop(test) && <button className="danger-outline-button" onClick={onStop} disabled={busy}><Pause size={15} /> Остановить рекламу</button>}</div></div>
      <div className="ab-summary-metrics"><SummaryMetric label="Кол-во фото" value={String(test.variants.length)} /><SummaryMetric label="Показов на одно фото" value={formatNumber(test.views_per_variant)} /><SummaryMetric label="Расход" value={formatRub(test.total_spend_rub)} /><SummaryMetric label="CTR по кампании" value={test.total_views ? formatPercent(campaignCtr) : '—'} /><SummaryMetric label="CPO кампании" value={formatOptionalRub(test.total_cpo)} /></div>
    </section>
  </>;
}
function FinalResultsSection({ test, variants, winner }: { test: ABTest; variants: ABTestVariant[]; winner: ABTestVariant | null }) {
  const ordered = [...variants].sort((left, right) => left.position - right.position);
  const isRunning = test.status === 'running' && test.campaign_state === 'running' && !requiresReconciliation(test);
  const isFinished = test.status === 'finished';
  const statsAreAggregate = test.stats_quality !== 'stage_attributed';
  const totalCtr = test.total_views
    ? (test.total_ctr != null && Number.isFinite(test.total_ctr) ? test.total_ctr : (test.total_clicks / test.total_views) * 100)
    : 0;
  const bannerTitle = isFinished && winner && !statsAreAggregate ? 'Победитель найден' : testStatusLabel(test);
  const bannerText = isFinished && winner && !statsAreAggregate
    ? 'Лучший вариант определён по подтверждённой статистике. Исходные фото восстановлены; победитель не применяется автоматически.'
    : moduleActionLabel(test);
  const bannerNote = decisionLabel(test.winner_decision);
  return <section className="ab-final-results">
    <div className={`ab-final-banner ${isFinished && winner ? 'complete' : 'pending'}`}><div><h2>{bannerTitle}</h2><p>{bannerText}</p><small>{bannerNote}</small></div></div>
    {statsAreAggregate && <div className="ab-inline-warning"><CircleAlert size={15} /> CTR кампании: <strong>{test.total_views ? `${totalCtr.toFixed(2)}%` : '—'}</strong> ({formatNumber(test.total_clicks)} кликов из {formatNumber(test.total_views)} показов). CTR отдельных фото скрыт: разбивка по фото не подтверждена. Эти данные не определяют победителя.</div>}
    {(test.unallocated_views > 0 || test.unallocated_clicks > 0 || test.unallocated_spend_rub > 0) && <div className="ab-inline-warning"><CircleAlert size={15} /> Не распределено по фото: {formatNumber(test.unallocated_views)} показов, {formatNumber(test.unallocated_clicks)} кликов, {formatRub(test.unallocated_spend_rub)}.</div>}
    <div className="ab-final-cards">{ordered.map((variant) => {
      const isWinner = Boolean(winner && variant.id === winner.id);
      const isCurrent = isRunning && variant.position === test.current_variant_order;
      const badge = isWinner ? 'Лучший' : isCurrent ? 'Сейчас в тесте' : isFinished ? 'Этап завершён' : variant.views > 0 ? 'Собрано' : 'Ожидает';
      const ctr = !statsAreAggregate && variant.views ? formatPercent(variant.ctr) : '—';
      const variantLabel = variant.position === 0 ? 'Главное фото' : `Фото ${variant.position}`;
      return <article className={`ab-final-card ${isWinner ? 'winner' : ''}`} key={variant.id}>
        <div className="ab-final-image">{variant.image_url ? <AuthenticatedImage src={imageUrl(variant.image_url)} alt={variantLabel} /> : <span><Clock3 size={24} /></span>}<b className={isCurrent ? 'current' : ''}>{badge}</b></div>
        <div className="ab-final-card-body"><strong>{variantLabel}</strong><em>{ctr}</em><div className="ab-final-card-stats"><span><Eye size={13} /> Показы: {formatNumber(variant.views)}</span><span>Клики: {formatNumber(variant.clicks)}</span></div></div>
      </article>;
    })}</div>
    <div className="ab-comparison-wrap"><table className="ab-comparison-table"><thead><tr><th>Показатель</th>{ordered.map((variant) => <th key={variant.id}>{variant.position === 0 ? 'Главное фото' : `Фото ${variant.position}`}</th>)}</tr></thead><tbody>
      <tr><th>Статус</th>{ordered.map((variant) => { const isWinner = Boolean(winner && variant.id === winner.id); const isCurrent = isRunning && variant.position === test.current_variant_order; const status = isWinner ? 'Лучший' : isCurrent ? 'В тесте' : isFinished ? 'Этап завершён' : variant.views > 0 ? 'Собрано' : 'Ожидает'; return <td key={variant.id}><span className={`ab-final-status ${isWinner ? 'winner' : isCurrent ? 'current' : ''}`}>{status}</span></td>; })}</tr>
      <tr><th>Показов</th>{ordered.map((variant) => <td key={variant.id}>{formatNumber(variant.views)}</td>)}</tr>
      <tr><th>Кликов</th>{ordered.map((variant) => <td key={variant.id}>{formatNumber(variant.clicks)}</td>)}</tr>
      <tr><th>CTR</th>{ordered.map((variant) => { const isWinner = Boolean(winner && variant.id === winner.id); return <td className={isWinner ? 'emphasis' : ''} key={variant.id}>{!statsAreAggregate && variant.views ? formatPercent(variant.ctr) : '—'}</td>; })}</tr>
      <tr><th>В сравнении с лучшим CTR в тесте</th>{ordered.map((variant) => { const isWinner = Boolean(winner && variant.id === winner.id); return <td key={variant.id}>{winner ? (isWinner ? <strong className="ab-best-cell"><Trophy size={14} /> Лучший</strong> : `${relativeToWinner(variant, winner)}%`) : '—'}</td>; })}</tr>
    </tbody></table></div>
    <section className="ab-additional-metrics"><div className="ab-additional-heading"><div><h3>Дополнительные показатели</h3><p>Ключевые показатели эффективности рекламной кампании.</p></div><span>Данные WB</span></div><div className="ab-additional-grid"><div className="ab-additional-metric"><small>CPC кампании</small><strong>{formatDecimalRub(calculateCpc(test.total_spend_rub, test.total_clicks))}</strong><span>стоимость клика</span></div><div className="ab-additional-metric"><small>Заказы</small><strong>{formatNumber(test.total_orders)}</strong><span>по кампании</span></div><div className="ab-additional-metric"><small>CR кампании</small><strong>{formatPercent(calculateCr(test.total_orders, test.total_clicks) || 0)}</strong><span>заказы из кликов</span></div><div className="ab-additional-metric"><small>CPM факт</small><strong>{formatDecimalRub(calculateCpm(test.total_spend_rub, test.total_views))}</strong><span>за 1 000 показов</span></div></div></section>
  </section>;
}
function relativeToWinner(variant: ABTestVariant, winner: ABTestVariant) {
  if (!winner.ctr || variant.ctr == null) return '—';
  const value = ((variant.ctr - winner.ctr) / winner.ctr) * 100;
  return `${value.toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}`;
}
function SettingsIcon() { return <span className="ab-settings-icon">⚙</span>; }
