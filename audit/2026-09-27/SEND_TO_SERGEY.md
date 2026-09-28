# Sergeyga yuboriladigan qayta audit paketi

## Tekshirilgan versiya

- Bazaviy commit: `7d3f7f4e50f76a8a39024879367abc719dbe6c6a`
- Audit sanasi: `2026-09-27`–`2026-09-28`
- Arxivdagi kod: shu loyiha katalogining audit vaqtida olingan snapshoti
- `.env`, tokenlar, parollar, real WB kabineti va real mablag‘ kiritilmagan

## Natija

- 64 requirement: **51 yopilgan, 13 qisman, 0 ochiq**.
- Backend: **77 test passed**.
- Frontend production build: **passed**.
- Frontend browser acceptance: **12 passed**.
- Sintetik PostgreSQL audit: **7/7 scenario passed** (`run-20260928-004840`).
- Real WB pilot hali bajarilmagan; 13 ta qisman band provider kontrakti, real WB yoki policy kelishuviga bog‘liq.

## Asosiy fayllar

- `audit/2026-09-27/REPORT.md` — yakuniy qisqa hisobot.
- `audit/2026-09-27/FULL-MATRIX.md` — 64 ID bo‘yicha to‘liq jadval.
- `audit/2026-09-27/ATTRIBUTION-CONTRACT.md` — aggregate statistikadagi attribution chegarasi.
- `audit/2026-09-27/results/run-20260928-004840/` — yakuniy 7/7 sintetik run loglari va natijalari.
- `audit/2026-09-27/run.py` va `audit/2026-09-27/harness/` — auditni qayta ishga tushirish vositalari.

## Qayta tekshirish

```bash
venv/bin/python -m pytest -c backend/pytest.ini backend/tests -q
cd frontend && npm ci && npm run build
cd ..
npx --prefix frontend playwright test --config=frontend/playwright.config.ts
AUDIT_CASES=control,stale_confirmation,overlap,repeat_start,pause_failure,external_edit,retry_photo venv/bin/python audit/2026-09-27/run.py
```

## Sergeyga yuboriladigan izoh

Bu paket 64 band bo‘yicha qayta audit va tuzatishlar dalilidir. `51/13/0` natijasi lokal kod, test va loopback sintetik WB asosida olingan. Bu hali real Wildberries kabinetida qabul qilish degani emas. Qisman bandlar bo‘yicha Sergey provider kontrakti, policy qarori yoki alohida real WB pilotini belgilashi kerak.
