# WB Optimizer

FastAPI + SQLAlchemy async + PostgreSQL backend and React + TypeScript frontend for Wildberries promotion statistics and image A/B tests.

## Included

- Email registration with a six-digit verification code.
- Login, access JWT and rotating refresh tokens.
- Logout and refresh-token revocation.
- Forgot-password and reset-password flows.
- Authenticated password change.
- Encrypted Wildberries token storage.
- WB Content and Promotion access checks for A/B tests; Analytics and Statistics remain optional because this product does not use them for the experiment decision.
- Responsive modern dashboard and settings UI.
- Editable profile details from the user menu.
- Multiple named Wildberries store connections per user with active-store switching.
- Dashboard statistics for today, 7 days, 30 days and a custom period.
- A/B workflow for 2–5 images: draft, campaign creation, sequential swap switching, WB fullstats, CTR-only winner decision and rollback.
- Cursor pagination for the Wildberries card catalog.
- Alembic migrations through `0012_admin_audit_logs`.
- Durable A/B operation journal, idempotent launch confirmation and explicit incident reconciliation.

## Local start

1. Copy `.env.example` to `.env` and set a strong `JWT_SECRET_KEY`.
2. Start PostgreSQL:

   ```bash
   docker compose up -d postgres
   ```

3. Install backend dependencies and migrate:

   ```bash
   ./venv/bin/python -m pip install -r requirements.txt
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/wb_optimizer \
     PYTHONPATH=backend ./venv/bin/python -m alembic -c backend/alembic.ini upgrade head
   ```

4. Start the backend:

   ```bash
   PYTHONPATH=backend ./venv/bin/python -m uvicorn app.main:app --reload --app-dir backend
   ```

5. Start the frontend in another terminal:

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

When SMTP is empty in development, verification and reset codes are printed in the backend log. Configure SMTP before deploying.

The application must be migrated before the backend is started. `create_all` is useful only for a clean local database and does not alter an existing schema:

```bash
PYTHONPATH=backend ./venv/bin/python -m alembic -c backend/alembic.ini upgrade head
```

For production set `AUTO_CREATE_TABLES=false`, use a strong `JWT_SECRET_KEY`, configure `FERNET_KEY`, SMTP and a separate `MEDIA_SIGNING_SECRET`. WB API keys are stored encrypted and are sent to WB using the official `HeaderApiKey` format (the token itself, without `Bearer`).

Backend docs: http://localhost:8000/docs
Frontend: http://localhost:5173

## API surface

- `POST /api/auth/register/start`
- `POST /api/auth/register/verify`
- `POST /api/auth/register/resend`
- `POST /api/auth/login`
- `POST /api/auth/refresh`
- `POST /api/auth/logout`
- `GET /api/auth/me`
- `PATCH /api/auth/profile`
- `POST /api/auth/change-password`
- `POST /api/auth/forgot-password`
- `POST /api/auth/reset-password`
- `GET /api/wb/token`
- `GET /api/wb/connections`
- `POST /api/wb/connections`
- `POST /api/wb/token`
- `POST /api/wb/token/validate`
- `POST /api/wb/connections/{connection_id}/validate`
- `DELETE /api/wb/token`
- `DELETE /api/wb/connections/{connection_id}`
- `GET /api/wb/dashboard?connection_id={id}&period=today`
- `GET /api/wb/dashboard?connection_id={id}&period=custom&begin_date=YYYY-MM-DD&end_date=YYYY-MM-DD`

### A/B tests

- `GET /api/ab-tests/cards?connection_id={id}&search=...`
- `GET /api/ab-tests/cards/{nm_id}?connection_id={id}`
- `GET /api/ab-tests`
- `POST /api/ab-tests`
- `POST /api/ab-tests/{id}/variants/{position}`
- `POST /api/ab-tests/{id}/start`
- `POST /api/ab-tests/{id}/sync`
- `POST /api/ab-tests/{id}/stop`
- `POST /api/ab-tests/{id}/reconcile`
- `POST /api/ab-tests/{id}/reconcile`
- `DELETE /api/ab-tests/{id}`

WB's Content API replaces a media file by its ordinal `X-Photo-Number` when `/content/v3/media/file` is called. Before an experiment, the service downloads and stores every original photo slot. A card photo is applied with a two-way swap (`slot N → slot 1` and the active `slot 1 → slot N`); a custom upload uses an unused parking slot when available. Upload success is treated as the provider's authoritative write result; this implementation intentionally does not add digest/fingerprint or CDN read-back validation. A pending media state is persisted so an interrupted write can be reconciled without blindly repeating an uncertain request. Campaign stop and media restore are verified independently. Before depositing money or starting a campaign, the service also reads WB's `/api/advert/v2/adverts` details and requires the selected `nm_id` to be present; an empty or mismatched campaign is blocked and never reused automatically. The test cannot start with fewer than two tested images or more than five, and simultaneous running tests for the same user/card are blocked at the application and PostgreSQL levels. Fullstats requests are rate-limited seller-wide, and campaign-level metrics are never presented as proven per-photo CTR. Before an automatic campaign top-up, the service reads `/adv/v1/balance` and uses the user-selected source. If the result is uncertain, a second charge is blocked and the existing WB campaign must be reconciled before continuing.
