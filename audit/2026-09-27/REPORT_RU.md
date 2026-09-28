# Повторный аудит A/BTEST — 27–28 сентября 2026 г.

Этот отчёт сопоставляет исправленную версию с 64 критериями и 11 группами замечаний из пакета `abtest-developer-package-7d3f7f4-20260926`. Реальный кабинет Wildberries и реальные деньги не использовались: действия WB выполнялись на синтетическом loopback-сервере.

## Подтверждённые результаты

- Backend unit/regression suite: **77 passed** (`backend/tests`).
- Production build frontend: **passed** (`npm run build`).
- Browser acceptance frontend: **12 passed** (`frontend/tests/acceptance.spec.ts`).
- Финальный синтетический прогон PostgreSQL: **7/7 passed** — `control`, `stale_confirmation`, `overlap`, `repeat_start`, `pause_failure`, `external_edit`, `retry_photo`; результат сохранён в `results/run-20260928-004840/execution.json`.
- Миграции 0013–0020 применяются на новой базе; добавлены incident notification outbox, funding/spend ledger и fixed-point money columns.
- `draft_fingerprint` обязателен: старое подтверждение после изменения черновика отклоняется с HTTP 409.
- Lifecycle-lock для одного seller/card действует между пользователями; второй тест блокируется до смены media или внешней кампании.
- Retry после ошибки старта повторно использует существующую кампанию; idempotency key не допускает повторного deposit/campaign.
- Pause/stop считаются успешными только после подтверждённого WB non-serving; unresolved campaign возвращается в planner safety sweep.
- Media snapshot хранит число фото, URL/hash и состояние видео; внешнее изменение продавца блокирует restore.
- Truncated JPEG не проходит полный decode; TIFF повторно валидируется после конвертации.
- Добавлены проверки seller-info, token permissions и write access; токен хранится зашифрованным и маскируется в логах.
- В UI неопределённые pause/reupload/error не скрываются под Active/Running; неподтверждённый остаток бюджета и aggregate CTR помечаются как неопределённые.
- Каждый scheduler-тест использует отдельную `AsyncSession`; медленная операция одного теста не блокирует safety stop других.
- После успешного `retry_photo` snapshot URL/hash обновляется; исправлена ошибка, из-за которой собственная загрузка ошибочно считалась внешним изменением.
- Photo picker получил явные accessibility-имена; сценарий выбора `Фото 2` стабильно проходит.

## Итог по 64 критериям

**51 закрыт, 13 частично подтверждены, 0 открыт.** Полная таблица находится в [FULL-MATRIX.md](FULL-MATRIX.md). Частичные пункты требуют provider contract, пилота на реальном WB или решения заказчика по policy.

## Ограничения, которые нельзя считать закрытыми

Обычный WB `fullstats` не доказывает, к какой фотографии относятся показы. Программа выбирает winner только при явном `variantPosition`; aggregate ответ получает статус `aggregate_unverified`, и winner не записывается. Контракт приведён в [ATTRIBUTION-CONTRACT.md](ATTRIBUTION-CONTRACT.md).

Реальный остаток бюджета, задержки CDN, права и advertisability в кабинете WB, а также provider attribution не могут быть полностью доказаны синтетическим сервером. Поэтому результат 51/13/0 не означает готовность к безусловному запуску на реальном кабинете.

## Команды повторной проверки

```bash
venv/bin/python -m pytest -c backend/pytest.ini backend/tests -q
cd frontend && npm run build
cd ..
npx --prefix frontend playwright test --config=frontend/playwright.config.ts
AUDIT_CASES=control,stale_confirmation,overlap,repeat_start,pause_failure,external_edit,retry_photo venv/bin/python audit/2026-09-27/run.py
```

Синтетические логи находятся в `results/`, финальный прогон — `run-20260928-004840`. Harness для повторного запуска — `run.py`.

## Решение о приёмке

Кодовые защиты и синтетические сценарии для критичных остановок рекламы, денег и media повторно подтверждены. Окончательная приёмка для реального WB требует согласовать частичные пункты и провести отдельный пилот с контролируемыми карточкой и бюджетом.
