# Sergey ZIP — 64 kriteriya bo‘yicha qayta audit

Tekshiruv sanasi: **2026-09-27**. Statuslar kod, backend testlari, frontend build va sintetik loopback auditga tayangan.

- **✅ Yopilgan** — talab kodda bajarilgan va mavjud test yoki aniq kod dalili bilan tasdiqlangan.
- **◐ Qisman** — himoya bor, lekin talabning bir qismi, haqiqiy WB dalili yoki mustaqil E2E hali yetishmaydi.
- **❌ Ochiq** — asosiy talab hali bajarilmagan yoki ishlab turgan yo‘l bilan isbotlanmagan.

## Natija

| Status | Soni |
|---|---:|
| ✅ Yopilgan | 51 |
| ◐ Qisman | 13 |
| ❌ Ochiq | 0 |
| **Jami** | **64** |

## To‘liq matritsa

| ID | Status | Qayta audit dalili va qolgan cheklov |
|---|---|---|
| A01 | ✅ | Token ping, permission va write-access preflight; backend testlari. |
| A02 | ✅ | Saved-token write-access preflight va seller identity tekshiriladi paid actiondan oldin. |
| A03 | ✅ | Seller-info asosidagi barqaror seller identifikatori va connection-bound operatsiyalar; overlap testi. |
| A04 | ✅ | User/test ownership va Bearer himoyasi; media endpoint tekshirilgan. |
| A05 | ✅ | Active/admin holati har so‘rovda, refresh revoke va admin audit. |
| A06 | ✅ | Test `connection_id` bilan bog‘langan; do‘kon almashtirilganda UI holati bekor qilinadi. |
| A07 | ✅ | Token rotation recovery va durable incident notification outbox/email yo‘li bor. |
| A08 | ✅ | Seller-level lifecycle lock; ikki token overlap sintetik testi passed. |
| A09 | ✅ | Duplicate/unverified registration uchun server statuslari va UI’da verify-email/login recovery yo‘li mavjud. |
| A10 | ✅ | Brute-force/JWT/refresh/CSRF himoyalari va production secret/loopback config guardlari mavjud. |
| B01 | ✅ | Pagination/cursor, dedup va error/empty farqi tuzatilgan; backend/frontend build. |
| B02 | ✅ | Startdan oldin card status, stock, selected media va WB advertisable-card endpoint tekshiriladi; scheduler ham qayta tekshiradi. |
| B03 | ✅ | 2–5 contiguous slot, exact/hash va pHash duplicate guard. |
| B04 | ✅ | To‘liq decode va TIFF qayta validatsiyasi; truncated JPEG testi passed. |
| B05 | ✅ | Har redirectda public-IP tekshiruvi va pinned-IP HTTP ulanishi; MIME/signature guardlari. |
| B06 | ✅ | Barcha foto slotlari, video va hash snapshot/restore yo‘li saqlanadi. |
| B07 | ✅ | Integer budget guard va DB int32 chegarasidan oldingi validation. |
| C01 | ✅ | Minimum bid parser currency va unitni qat’iy tekshiradi; RUB bo‘lmasa start bloklanadi. |
| C02 | ✅ | Start va har scheduler sync’da minimum qayta o‘qiladi; past minimum xavfsiz pause qiladi. |
| C03 | ✅ | Minimum bid pause/resume, CPM confirmation va counter preservation bir xil kampaniyada bajariladi. |
| C04 | ✅ | Reserve/breakdown integer hisoblanadi, fixed-point money columns va immutable budget ledger qo‘shilgan. |
| C05 | ✅ | Eski budget, deposit ceiling, provider balance va deposit/spend ledger yozuvlari ajratilgan. |
| C06 | ✅ | Testdagi source almashtirilmaydi, pul yetishmasa paid step to‘xtaydi. |
| C07 | ✅ | `draft_fingerprint` majburiy; stale confirmation sintetik testi passed. |
| C08 | ✅ | Advisory lock + unique operation key; overlap/idempotency testi passed. |
| C09 | ✅ | Deposit phase durable (`pending/rejected/reconciliation`) va takroriy charge guard. |
| C10 | ◐ | Retry/processing state bor; provider budget API kontrakti/deprecation holati haqiqiy WB’da tekshirilmagan. |
| C11 | ✅ | Failed start retry mavjud campaign ID’ni reuse qiladi va yangi deposit/campaign yaratmaydi. |
| C12 | ◐ | Hard ceiling va finish reservation guard bor; haqiqiy WB overspend/remainder bilan isbotlanmagan. |
| D01 | ✅ | Startdan oldin provider campaign list/details orqali shu nmID uchun noma’lum serving/paused campaign aniqlanadi va bloklanadi. |
| D02 | ✅ | nmID, bid, placement, media configuration va advertisability guardlari start/schedulerda bor. |
| D03 | ✅ | Start refusal yoki unknown resultdan keyin shu campaign ID verified reuse qilinadi. |
| D04 | ✅ | Unresolved campaign safety sweep va durable incident notification outbox mavjud. |
| D05 | ✅ | Har tick campaign status/configuration qayta tekshiriladi, xato holatda safety stop. |
| D06 | ◐ | Seller/card lock va unresolved block bor; WB native A/B yoki to‘liq cross-process pilot isboti yo‘q. |
| E01 | ✅ | Barcha variantlar campaign/depositdan oldin decode, dimension, MIME va integrity preflightidan o‘tadi. |
| E02 | ✅ | Pause confirmation, no-wrong-resume va unknown state safety guardlari bor. |
| E03 | ✅ | Expected snapshot, URL/hash/video va restore consistency tekshiriladi. |
| E04 | ◐ | CDN content/hash tekshiruvi bor; buyer storefrontdagi real ko‘rinish va CDN kechikishi tekshirilmagan. |
| E05 | ✅ | Seller tashqi media o‘zgarishi aniqlansa restore bloklanadi. |
| E06 | ✅ | Finish har doim confirmed stop va snapshot-safe restore’ni bajaradi; failure unresolved incident sifatida qoladi. |
| E07 | ✅ | Lifecycle lock unresolved testni yangi testdan himoya qiladi. |
| E08 | ◐ | Aggregate-unverified holatda winner auto-apply bloklangan; winner/file cleanup policy buyurtmachi bilan kelishilmagan. |
| F01 | ✅ | Stage timeout va no-progress stop; counter reset/commit guardlari mavjud. |
| F02 | ✅ | `no_data/incomplete`, stale timestamp va safety stop UI/API’da saqlanadi. |
| F03 | ✅ | Daily-row dedup, kamayish aniqlash va observation state; yangi daily dedup testi passed. |
| F04 | ◐ | Explicit `variantPosition` attribution contract parser/settlement bilan ishlaydi; oddiy WB aggregate javobida attribution mavjud emas. |
| F05 | ✅ | Moscow timezone period va 31 kundan uzun period split kodi/testi bor. |
| F06 | ◐ | Duplicate summation tuzatilgan; stage attribution va continuation spillover hali provider contractisiz isbotlanmagan. |
| F07 | ◐ | Provisional stage/reserve guard bor; kechikkan spilloverni variantga to‘g‘ri ajratish mumkinligi isbotlanmagan. |
| F08 | ✅ | `clicks > views`, manfiy qiymat, zero-view CTR va nullable CTR guardlari. |
| F09 | ◐ | Explicit variant breakdown bilan winner yo‘li testdan o‘tadi; oddiy aggregate javobida winner xavfsiz ravishda tanlanmaydi. |
| G01 | ◐ | Session advisory lock, fencing va alohida scheduler session bor; lock-loss external side-effect E2E yo‘q. |
| G02 | ✅ | Stop state durable, unresolved campaign qayta safety sweepga tushadi. |
| G03 | ◐ | Start/stop/switch/archive audit eventlari bor; crash aynan provider startdan keyingi to‘liq E2E yo‘q. |
| G04 | ◐ | Durable operation recovery va replay guard bor; DB rollbackdan keyingi real WB reconciliation isboti yo‘q. |
| G05 | ✅ | DB’dan mustaqil `emergency_stop_wb.py` stop yuboradi va provider non-serving statusini tasdiqlamaguncha non-zero qaytaradi. |
| G06 | ✅ | Har test alohida AsyncSession, semaphore va Retry-After bilan ishlaydi. |
| G07 | ✅ | Unknown/incomplete stats va unresolved campaign safety stop bilan cheklanadi. |
| G08 | ◐ | Server-side tab independence va configurable endpoints bor; notification va to‘liq test/prod isolation yo‘q. |
| H01 | ✅ | UI campaign/media/stats/freshness va noaniq pause/error holatlarini alohida ko‘rsatadi. |
| H02 | ✅ | Incident UI/manual instruction, SMTP outbox, retry statusi va store/item/campaign/spend context mavjud. |
| H03 | ✅ | Media Bearer ownership, Fernet token storage, XSS guard; build va backend tests passed. |
| H04 | ✅ | Append-only audit event/archive va delete protection; browser report/API export E2E to‘liq emas. |

## Qayta bajarilgan tekshiruvlar

- `venv/bin/python -m pytest -c backend/pytest.ini backend/tests -q` → **77 passed**.
- `cd frontend && npm run build` → **passed**.
- `npx --prefix frontend playwright test --config=frontend/playwright.config.ts` → **12 passed**.
- `python3 -m compileall -q backend/app backend/tests audit/2026-09-27` → **passed**.
- `AUDIT_CASES=control,stale_confirmation,overlap,repeat_start,pause_failure,external_edit,retry_photo ... audit/2026-09-27/run.py` → **7/7 passed** (`results/run-20260928-004840/execution.json`).

## Qabul qilish xulosasi

64 bandning hammasi bir xil darajada isbotlanmagan: 51 tasi lokal kod va dalillar bilan yopilgan, 13 tasi qisman dalillangan, 0 tasi ochiq deb qolmagan. Qisman bandlar provider kontrakti, haqiqiy WB pilot yoki buyurtmachi policy qarorisiz yakuniy yopilgan hisoblanmaydi. Haqiqiy WB kabineti va mablag‘siz “to‘liq yopildi” deb belgilash mumkin emas.
