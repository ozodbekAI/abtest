# Результаты повторной проверки

Дата: 13.10.2026.

- Backend: **135 passed** (`backend/pytest`), включая late-click CTR interval, near-quota budget guard и fail-closed winner.
- Python compile check: `python -m compileall -q backend/app` — PASS.
- Frontend: `npm run build` — PASS (TypeScript + Vite production bundle).
- Dependency audit: `npm audit --audit-level=high` — **0 vulnerabilities**.
- Dependency tree: `source-map-js@1.2.2` установлен через `overrides`.
- Полный F01–F09 mock-WB прогон до этого набора правок был сохранён в отдельной поставке; в этой повторной проверке F06 запущен на чистом стенде после правок. При 600 показах/мин этап дошёл до 1350 показов и не остановился на старом пороге 950, после чего стенд был остановлен во время штатного 10-минутного settle/трёх snapshot окна. Поэтому финальный пост-патч F01–F09 verdict не заявляется как завершённый.

## Интерпретация

`stage_attributed` означает variant-level подтверждение WB. `stage_estimated`, `data_unstable` и `post_stop_unallocated` показываются как оценка/нераспределённые данные; такие данные не могут автоматически выбрать победителя.

## Ограничение доказательств

Кодовые замечания аудита закрыты и покрыты acceptance-тестами. Полный post-patch F01–F09 прогон и реальный платный WB pilot в этой проверке не завершены; перед коммерческим включением их нужно выполнить отдельно.

