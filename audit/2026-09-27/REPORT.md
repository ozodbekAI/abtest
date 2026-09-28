# A/BTEST qayta audit — 2026-09-27–28

Bu hisobot foydalanuvchi bergan `abtest-developer-package-7d3f7f4-20260926` ichidagi 64 mezon va 11 ta asosiy kamchilik ro‘yxati bilan solishtirib tayyorlandi. Tekshiruv haqiqiy WB kabineti yoki haqiqiy mablag‘ ishlatmagan; WB harakatlari loopback sintetik serverda bajarilgan.

## Tasdiqlangan natijalar

- Backend unit/regression suite: **77 passed** (`backend/tests`), jumladan rasm yaxlitligi, pHash/dublikat, daily-stat dedup, explicit variant attribution, idempotency, stale confirmation, lifecycle lock, media snapshot, winner safety va budget guard.
- Frontend production build: **passed** (`npm run build`).
- Frontend browser acceptance: **12 passed** (`frontend/tests/acceptance.spec.ts`).
- API qayta tekshiruvi: yakuniy 7 ta sintetik scenario (`results/run-20260928-004840/execution.json`) — **7/7 passed**: `control`, `stale_confirmation`, `overlap`, `repeat_start`, `pause_failure`, `external_edit`, `retry_photo`.
- 0013–0020 migratsiyalari yangi bazada qo‘llanadi; incident notification outbox, funding/spend ledger va fixed-point money columns ham migratsiya bilan qo‘shildi; uzun Alembic revision identifikatorlari uchun version ustuni 128 belgiga kengaytirildi.
- `draft_fingerprint` endi majburiy: draft o‘zgarganidan keyin eski launch tasdig‘i 409 bilan rad qilinadi.
- Bitta seller/card uchun lifecycle lock barcha foydalanuvchilar bo‘yicha ishlaydi; ikkinchi test tashqi kampaniya yoki media o‘zgarishidan oldin bloklanadi.
- Start xatosidan keyingi retry mavjud kampaniyani qayta ishlatadi; idempotency key takroriy deposit/campaign yaratmaydi.
- Pause/stop faqat WB qaytargan non-serving holat tasdiqlangandan keyin muvaffaqiyatli deb belgilanadi; unresolved kampaniyalar planner safety sweep’ga qaytadi.
- Media snapshot foto soni, URL/hash va video holatini tekshiradi; tashqi sotuvchi o‘zgarishi aniqlansa restore yozuvi bloklanadi.
- Truncated JPEG to‘liq decode’dan o‘tmaydi; TIFF JPEG’ga aylantirilgandan keyin ham qayta validatsiya qilinadi.
- `seller-info`, token permission va write access tekshiruvi qo‘shilgan; token shifrlangan saqlanadi va loglarda maskalanadi.
- UI’da noaniq pause/reupload/error holatlari “Active/Running” ko‘rinishida yashirilmaydi; tasdiqlanmagan budget remainder va aggregate CTR noaniq deb ko‘rsatiladi. Incident outbox SMTP orqali qayta urinadi; budget ledger deposit, provider balance va spend snapshot’ni ajratadi.
- Har bir scheduler testi alohida `AsyncSession` bilan ishlaydi; bitta testdagi sekin yoki xato operatsiya boshqalar safety stop’ini bloklamaydi.
- Lokal PostgreSQL migration chain `0020_money_as_numeric (head)`gacha qo‘llandi; legacy empty seller fingerprints sababli unique-index migration failure ham tuzatildi.
- `retry_photo` qayta yuklash yo‘lida muvaffaqiyatli variant o‘zgarishidan keyin media URL/hash snapshot yangilanadigan qilindi; aks holda keyingi stop o‘zimiz yozgan slotlarni tashqi o‘zgarish deb noto‘g‘ri bloklar edi.
- Photo picker tugmalariga aniq accessibility nomi berildi; frontend acceptance’dagi `Фото 2` tanlash ssenariysi endi barqaror ishlaydi.

64 ta talabning ID-bo‘yicha to‘liq jadvali [FULL-MATRIX.md](FULL-MATRIX.md) faylida. Qayta audit natijasi: **51 ta yopilgan, 13 ta qisman, 0 ta ochiq**. Qisman bandlar provider contract, haqiqiy WB pilot yoki buyurtmachi bilan policy kelishuvini talab qiladi.

## Hali “yopildi” deb hisoblanmaydigan mezonlar

WB aggregate `fullstats` javobi ko‘rsatishlarni qaysi foto bosqichiga tegishli ekanini ishonchli isbotlamasa, dastur variantga CTR yoki g‘olibni taxminan yozmaydi. Endi provider explicit `variantPosition` qaytarganida attribution va winner yo‘li ishlaydi; oddiy aggregate javob esa `statistics_not_attributable`/`aggregate_unverified` bo‘lib qoladi. Contract tafsiloti [ATTRIBUTION-CONTRACT.md](ATTRIBUTION-CONTRACT.md)da.

Budjetning WB’dagi real qoldig‘i faqat provider tomonidan tasdiqlangan ledger/snapshot bilan ko‘rsatilishi mumkin; lokal `deposit` summasi real sarf yoki qaytariladigan qoldiq sifatida talqin qilinmaydi. C04/C05 shu chegaraga ega.

Haqiqiy WB’da permission, stock/advertisability, provider attribution va CDN kechikishlari sintetik serverdan to‘liq isbotlanmaydi. Sintetik `control` ssenariysi final run’da passed bo‘ldi, lekin uning mock-CND natijasi real WB storefront va real kechikishlarni isbotlamaydi. Natija `results/run-20260928-004840/` ichida saqlangan.

## Ishga tushirish

```bash
venv/bin/python -m pytest -c backend/pytest.ini backend/tests -q
cd frontend && npm run build
cd ..
npx --prefix frontend playwright test --config=frontend/playwright.config.ts
AUDIT_CASES=control,stale_confirmation,overlap,repeat_start,pause_failure,external_edit,retry_photo venv/bin/python audit/2026-09-27/run.py
```

Sintetik audit natijalari [results](results/) ichida, yakuniy run `run-20260928-004840`, qayta ishga tushirish harness’i [run.py](run.py) va frontend browser artefaktlari [frontend-browser-report](frontend-browser-report/) ichida. `run-20260928-002316` ichidagi avvalgi `retry_photo` xatosi tuzatishdan oldingi diagnostik yozuv sifatida saqlangan.

To‘liq ID matritsa: [FULL-MATRIX.md](FULL-MATRIX.md).

## Qabul qilish qarori

Kritik pul/média xavfsizliklari bo‘yicha oldingi 1–5 bandlar uchun kod himoyalari va ikki API ssenariysi qayta tasdiqlandi. Butun 64 talab bo‘yicha qabul qilish hali yakuniy “ready for real WB” emas: statistik attribution contract’i va haqiqiy WB pilot tekshiruvi alohida qolmoqda. Haqiqiy kabinet va mablag‘ bilan pilot faqat shu ochiq chegaralar bo‘yicha qaror qabul qilingandan keyin o‘tkazilishi kerak.
