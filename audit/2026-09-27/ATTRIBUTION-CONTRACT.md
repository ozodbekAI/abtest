# Statistik attribution contract

Oddiy WB `fullstats` javobi faqat kampaniya jami ko‘rsatkichlarini beradi. U variant fotosiga CTR bog‘lash uchun yetarli emas. AVEMOD endi faqat quyidagi aniq provider qatori mavjud bo‘lsa g‘olib hisoblaydi:

```json
[
  {"advertId": 700001, "variantPosition": 1, "views": 400, "clicks": 32, "orders": 0, "sum": 120.0},
  {"advertId": 700001, "variantPosition": 2, "views": 420, "clicks": 22, "orders": 0, "sum": 126.0}
]
```

`variantPosition` har bir satrda bo‘lishi, barcha metrikaning sonli va to‘liq bo‘lishi, `clicks <= views` bo‘lishi va satrlar campaign ID bilan mos kelishi shart. Bitta satr ham attribution’siz bo‘lsa, javob `aggregate_unverified` bo‘lib qoladi va winner yozilmaydi. Shu sabab oddiy WB aggregate javobidan noto‘g‘ri g‘olib chiqarish mumkin emas.

Koddagi parser bu contract’ni `_variant_totals` sifatida saqlaydi; settlement barcha variant pozitsiyalarini olgandagina `stage_attributed` holatiga o‘tadi. Test: `test_explicit_variant_attribution_is_preserved`, `test_explicit_variant_breakdown_enables_safe_winner`.
