# Контракт атрибуции статистики

Обычный ответ WB `fullstats` содержит только суммарные показатели кампании. Этого недостаточно, чтобы связать CTR с конкретной фотографией. AVEMOD считает winner только при наличии следующего provider-ряда:

```json
[
  {"advertId": 700001, "variantPosition": 1, "views": 400, "clicks": 32, "orders": 0, "sum": 120.0},
  {"advertId": 700001, "variantPosition": 2, "views": 420, "clicks": 22, "orders": 0, "sum": 126.0}
]
```

У каждой строки должен быть `variantPosition`; все метрики должны быть числовыми и полными; `clicks <= views`; строки должны соответствовать campaign ID. Если хотя бы одна строка не имеет attribution, ответ получает статус `aggregate_unverified`, и winner не записывается. Это предотвращает выбор победителя по неподтверждённому aggregate-ответу WB.

Parser хранит контракт как `_variant_totals`; settlement переходит в `stage_attributed` только после получения всех позиций вариантов. Тесты: `test_explicit_variant_attribution_is_preserved`, `test_explicit_variant_breakdown_enables_safe_winner`.
