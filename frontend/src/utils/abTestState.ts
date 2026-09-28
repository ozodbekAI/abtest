import type { ABTest } from '../types';

export function campaignStateLabel(value?: string | null) {
  return ({ not_created: 'Не создана', creating: 'Создаётся', created: 'Создана, не запущена', starting: 'Запуск ещё не подтверждён', running: 'Работает', pause_requested: 'Пауза запрошена, подтверждения WB нет', paused: 'Пауза подтверждена WB', stop_requested: 'Остановка запрошена, подтверждения WB нет', stopped: 'Остановка подтверждена WB', quarantined: 'Требуется ручная проверка', unknown: 'Состояние не подтверждено' } as Record<string, string>)[value || ''] || 'Состояние не подтверждено';
}

export function mediaStateLabel(value?: string | null) {
  return ({ original: 'Исходные фото', backed_up: 'Исходные фото сохранены в резерве', variant_applied: 'Тестовый вариант установлен', swapping: 'Проверяется замена фото', restoring: 'Исходные фото восстанавливаются', restored: 'Полный возврат оригинала подтверждён', winner_applied: 'Победитель установлен отдельным действием', waiting_image_reupload: 'Ожидает повторной загрузки фото', external_conflict: 'Конфликт внешних изменений', conflict_resolved: 'Конфликт разрешён пользователем', unknown: 'Состояние фото не подтверждено' } as Record<string, string>)[value || ''] || 'Состояние фото не подтверждено';
}

export function statsStateLabel(value?: string | null) {
  return ({ not_started: 'Данные ещё не получены', preliminary: 'Предварительные данные', stage_attributed: 'Подтверждена разбивка по этапам', incomplete: 'Неполные данные', reconciliation_required: 'Требуется сверка статистики', aggregate_unverified: 'Данные кампании без подтверждённой разбивки по фото' } as Record<string, string>)[value || ''] || 'Качество данных не подтверждено';
}

export function safetyClosed(test: ABTest) {
  return ['stopped', 'not_created'].includes(test.campaign_state)
    && ['original', 'restored', 'conflict_resolved'].includes(test.media_status);
}

export function needsReconciliation(test: ABTest) {
  return test.operation_state === 'reconciliation_required' || test.status === 'failed'
    || test.media_status === 'external_conflict'
    || (['finished', 'stopped'].includes(test.status) && !safetyClosed(test));
}

export function canSafelyStop(test: ABTest) {
  return test.status === 'running' || (!!test.wb_campaign_id && !safetyClosed(test));
}

export function testStatusLabel(test: ABTest) {
  if (test.media_status === 'external_conflict') return 'Конфликт внешних изменений';
  if (test.campaign_state === 'stop_requested') return 'Остановка запрошена, подтверждения WB нет';
  if (test.campaign_state === 'pause_requested') return 'Пауза запрошена, подтверждения WB нет';
  if (needsReconciliation(test)) return 'Требуется сверка операции';
  if (test.campaign_state === 'stopped' && !safetyClosed(test)) return 'Реклама остановлена, фото восстанавливаются';
  if (test.operation_state === 'awaiting_confirmation') return 'Ожидает подтверждения новых условий';
  if (test.campaign_state === 'paused') return test.media_status === 'waiting_image_reupload' ? 'Пауза: ожидается загрузка фото' : 'Пауза подтверждена WB';
  if (test.status === 'finished') return test.stats_quality === 'stage_attributed' ? 'Завершено' : 'Серия завершена, качество данных ограничено';
  if (test.status === 'stopped') return test.media_status === 'conflict_resolved' ? 'Прервано; конфликт разрешён пользователем' : 'Безопасно прервано';
  if (test.status === 'draft') return test.last_error ? 'Не запущено — проверьте условия' : 'Черновик';
  if (test.campaign_state === 'running') return 'Идёт тест';
  return 'Подготовка и проверка состояния';
}

export function decisionLabel(decision?: string | null) {
  return ({ winner_found: 'Победитель определён', no_clear_winner: 'Явного победителя нет', insufficient_data: 'Недостаточно данных', statistics_not_attributable: 'Нельзя надёжно распределить статистику по фото', test_interrupted: 'Серия прервана; победитель не определён' } as Record<string, string>)[decision || ''] || 'Результат сравнения ещё не подтверждён';
}

export function moduleActionLabel(test: ABTest) {
  if (test.media_status === 'external_conflict') return 'Автоматический возврат фото заблокирован, чтобы сохранить внешние изменения. Требуется решение по составу медиа.';
  if (!safetyClosed(test) && needsReconciliation(test)) return 'Новые варианты и пополнения заблокированы. Сервер продолжает проверки остановки и безопасного восстановления независимо от этой вкладки.';
  if (test.media_status === 'waiting_image_reupload') return 'Реклама на подтверждённой паузе. Сервер ожидает разрешённого повтора загрузки и проверит фото до запуска.';
  if (safetyClosed(test)) return 'Реклама не работает; обязательства по фото закрыты. Новые списания по этой серии не запрашиваются.';
  return 'Сервер проверяет состояние рекламной кампании, фото и статистики.';
}
