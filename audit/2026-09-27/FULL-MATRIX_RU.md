# Полная матрица повторного аудита — 64 критерия

Дата проверки: **27–28 сентября 2026 г.** Проверка выполнена по коду, backend-тестам, frontend build, browser acceptance и синтетическому loopback-WB.

- **ЗАКРЫТО** — требование реализовано и подтверждено тестом или проверяемым кодовым свидетельством.
- **ЧАСТИЧНО** — защита есть, но часть требования, доказательство на реальном WB или независимый E2E ещё отсутствует.
- **ОТКРЫТО** — основной путь не реализован или не доказан.

## Результат

| Статус | Количество |
|---|---:|
| ЗАКРЫТО | 51 |
| ЧАСТИЧНО | 13 |
| ОТКРЫТО | 0 |
| **Всего** | **64** |

## Полная таблица

| ID | Статус | Доказательство повторного аудита и ограничение |
|---|---|---|
| A01 | ЗАКРЫТО | Проверка token ping, разрешений и права записи до платных действий; подтверждено backend-тестами. |
| A02 | ЗАКРЫТО | Перед платным действием выполняются preflight права записи сохранённого токена и проверка идентичности продавца. |
| A03 | ЗАКРЫТО | Стабильный seller identity из seller-info и привязка операций к connection; пройден тест overlap. |
| A04 | ЗАКРЫТО | Проверены принадлежность пользователя/теста и Bearer-защита; проверен media endpoint. |
| A05 | ЗАКРЫТО | Состояние active/admin проверяется в каждом запросе; есть revoke refresh и admin audit. |
| A06 | ЗАКРЫТО | Тест связан с connection_id; смена магазина отменяет состояние черновика в интерфейсе. |
| A07 | ЗАКРЫТО | Есть восстановление после ротации токена и постоянная очередь уведомлений об инцидентах с email. |
| A08 | ЗАКРЫТО | Lifecycle-lock на уровне продавца; синтетический тест двух токенов прошёл. |
| A09 | ЗАКРЫТО | Для повторной/неподтверждённой регистрации есть серверные статусы и UI-восстановление через verify-email/login. |
| A10 | ЗАКРЫТО | Есть защита от brute-force, JWT/refresh/CSRF и проверки production secret/loopback-конфигурации. |
| B01 | ЗАКРЫТО | Исправлены pagination/cursor, dedup и различие error/empty; пройдены backend и frontend build. |
| B02 | ЗАКРЫТО | До старта и на каждом тике проверяются статус карточки, stock, выбранные media и advertisable-card endpoint WB. |
| B03 | ЗАКРЫТО | Проверяются 2–5 последовательных слотов, exact/hash и pHash-дубликаты. |
| B04 | ЗАКРЫТО | Полный decode и повторная проверка TIFF; тест truncated JPEG прошёл. |
| B05 | ЗАКРЫТО | На каждом redirect проверяется public IP, используется pinned-IP HTTP; есть MIME/signature guard. |
| B06 | ЗАКРЫТО | Сохраняются и восстанавливаются все photo slots, видео и hash snapshot. |
| B07 | ЗАКРЫТО | Есть integer budget guard и проверка границ DB int32. |
| C01 | ЗАКРЫТО | Parser minimum bid строго проверяет currency/unit; при валюте не RUB старт блокируется. |
| C02 | ЗАКРЫТО | Minimum bid перечитывается при старте и каждом scheduler sync; при недостатке выполняется безопасная пауза. |
| C03 | ЗАКРЫТО | Pause/resume при изменении minimum bid, CPM confirmation и сохранение счётчиков выполняются в одной кампании. |
| C04 | ЗАКРЫТО | Reserve/breakdown считается целыми числами; добавлены fixed-point money columns и неизменяемый budget ledger. |
| C05 | ЗАКРЫТО | Разделены старый budget, deposit ceiling, баланс провайдера и записи deposit/spend ledger. |
| C06 | ЗАКРЫТО | Исходник карточки тестом не подменяется; при недостатке денег платный шаг останавливается. |
| C07 | ЗАКРЫТО | draft_fingerprint обязателен; синтетический тест stale confirmation прошёл. |
| C08 | ЗАКРЫТО | Advisory lock и unique operation key; тест overlap/idempotency прошёл. |
| C09 | ЗАКРЫТО | Фазы deposit durable (pending/rejected/reconciliation), есть защита от повторного списания. |
| C10 | ЧАСТИЧНО | Retry/processing state есть; контракт и deprecation provider budget API на реальном WB не проверены. |
| C11 | ЗАКРЫТО | После отказа старта повторно используется существующий campaign ID без нового deposit/campaign. |
| C12 | ЧАСТИЧНО | Есть hard ceiling и finish reservation guard; overspend/remainder реального WB не доказаны. |
| D01 | ЗАКРЫТО | До старта список/details кампаний провайдера выявляют неизвестную serving/paused campaign для nmID и блокируют запуск. |
| D02 | ЗАКРЫТО | Проверки nmID, bid, placement, media configuration и advertisability выполняются при старте и на scheduler. |
| D03 | ЗАКРЫТО | После отказа старта или неизвестного результата повторно используется проверенный campaign ID. |
| D04 | ЗАКРЫТО | Есть safety sweep для unresolved campaign и постоянная очередь уведомлений об инциденте. |
| D05 | ЗАКРЫТО | На каждом тике повторно проверяются campaign status/configuration; при ошибке выполняется safety stop. |
| D06 | ЧАСТИЧНО | Seller/card lock и unresolved block есть; native WB A/B и полный cross-process pilot не доказаны. |
| E01 | ЗАКРЫТО | Все варианты до campaign/deposit проходят decode, dimension, MIME и integrity preflight. |
| E02 | ЗАКРЫТО | Есть pause confirmation, защита от wrong resume и unknown state safety guard. |
| E03 | ЗАКРЫТО | Проверяются expected snapshot, URL/hash/video и согласованность restore. |
| E04 | ЧАСТИЧНО | Проверка CDN content/hash есть; реальное отображение storefront и задержка CDN не проверены. |
| E05 | ЗАКРЫТО | При внешнем изменении media продавцом восстановление блокируется. |
| E06 | ЗАКРЫТО | Finish всегда подтверждает stop и выполняет snapshot-safe restore; сбой остаётся unresolved incident. |
| E07 | ЗАКРЫТО | Lifecycle lock защищает от нового теста при unresolved состоянии. |
| E08 | ЧАСТИЧНО | При aggregate-unverified auto-apply winner блокируется; policy winner/file cleanup не согласована с заказчиком. |
| F01 | ЗАКРЫТО | Есть stage timeout и остановка при отсутствии прогресса; guard сброса/фиксации счётчика. |
| F02 | ЗАКРЫТО | no_data/incomplete, stale timestamp и safety stop сохраняются в UI/API. |
| F03 | ЗАКРЫТО | Есть dedup daily-row, обнаружение уменьшения и observation state; новый daily dedup тест прошёл. |
| F04 | ЧАСТИЧНО | Parser/settlement explicit variantPosition работает; в обычном aggregate WB attribution отсутствует. |
| F05 | ЗАКРЫТО | Есть период в часовом поясе Москвы и разбиение периода длиннее 31 дня; код и тесты. |
| F06 | ЧАСТИЧНО | Duplicate summation исправлен; stage attribution и continuation spillover без provider contract не доказаны. |
| F07 | ЧАСТИЧНО | Есть provisional stage/reserve guard; корректное отнесение позднего spillover к варианту не доказано. |
| F08 | ЗАКРЫТО | Guard для clicks > views, отрицательных значений, zero-view CTR и nullable CTR. |
| F09 | ЧАСТИЧНО | Путь winner с explicit variant breakdown проходит тест; aggregate ответ безопасно не выбирает winner. |
| G01 | ЧАСТИЧНО | Есть session advisory lock, fencing и отдельная scheduler session; E2E потери lock с внешним side-effect нет. |
| G02 | ЗАКРЫТО | Stop state durable; unresolved campaign возвращается в safety sweep. |
| G03 | ЧАСТИЧНО | Есть audit events start/stop/switch/archive; полного E2E crash сразу после provider start нет. |
| G04 | ЧАСТИЧНО | Есть durable recovery и replay guard; reconciliation с реальным WB после DB rollback не доказана. |
| G05 | ЗАКРЫТО | Независимый от DB emergency_stop_wb.py отправляет stop и возвращает успех только после non-serving статуса провайдера. |
| G06 | ЗАКРЫТО | Каждый тест использует отдельную AsyncSession, semaphore и Retry-After. |
| G07 | ЗАКРЫТО | Unknown/incomplete stats и unresolved campaign ограничиваются safety stop. |
| G08 | ЧАСТИЧНО | Server-side tab independence и configurable endpoints есть; notification и полная test/prod isolation не доказаны. |
| H01 | ЗАКРЫТО | UI отдельно показывает campaign/media/stats/freshness и неясные pause/error состояния. |
| H02 | ЗАКРЫТО | Есть incident UI/manual instruction, SMTP outbox, retry status и контекст store/item/campaign/spend. |
| H03 | ЗАКРЫТО | Media Bearer ownership, хранение токена Fernet и XSS guard; build и backend tests прошли. |
| H04 | ЗАКРЫТО | Есть append-only audit event/archive и защита удаления; browser report/API export E2E неполный. |

## Повторно выполненные проверки

- `venv/bin/python -m pytest -c backend/pytest.ini backend/tests -q` → **77 passed**.
- `cd frontend && npm run build` → **passed**.
- `npx --prefix frontend playwright test --config=frontend/playwright.config.ts` → **12 passed**.
- `python3 -m compileall -q backend/app backend/tests audit/2026-09-27` → **passed**.
- `AUDIT_CASES=control,stale_confirmation,overlap,repeat_start,pause_failure,external_edit,retry_photo venv/bin/python audit/2026-09-27/run.py` → **7/7 passed** (`results/run-20260928-004840/execution.json`).

## Решение о приёмке

Из 64 пунктов 51 подтверждён локальным кодом и доказательствами, 13 подтверждены частично, открытых пунктов нет. Частичные пункты нельзя считать окончательно закрытыми без provider contract, пилота на реальном WB или согласованной policy. Реальный кабинет Wildberries и реальные деньги в проверке не использовались.
