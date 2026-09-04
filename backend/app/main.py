from contextlib import asynccontextmanager
import hashlib
import hmac
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.security import hash_password
from app.repositories.user_repository import UserRepository
from app.routers import ab_tests, admin, auth, health, wb
from app.services.ab_test_scheduler import ABTestScheduler

# Import models before create_all so SQLAlchemy knows every table.
from app import models  # noqa: F401,E402


MEDIA_ROOT = Path(settings.media_root).expanduser().resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)


async def ensure_configured_admin() -> None:
    """Promote or create the admin configured explicitly through the env.

    An empty ADMIN_EMAIL disables bootstrapping. Existing admin passwords are
    never overwritten on restart.
    """
    email = settings.admin_email.lower().strip()
    if not email:
        return
    async with AsyncSessionLocal() as db:
        users = UserRepository(db)
        user = await users.get_by_email(email)
        if user:
            if not user.is_admin:
                user.is_admin = True
                await db.commit()
            return
        if not settings.admin_password:
            return
        user = await users.create(
            email=email,
            hashed_password=hash_password(settings.admin_password),
            first_name="Администратор",
            last_name="Панели",
            is_verified=True,
            is_active=True,
            is_admin=True,
        )
        await db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_tables and settings.app_env != "production":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    await ensure_configured_admin()
    scheduler = ABTestScheduler()
    scheduler.start()
    yield
    await scheduler.stop()
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Авторизация, магазины Wildberries, статистика продвижения и A/B-тесты изображений.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Idempotency-Key"],
)

app.include_router(health.router)
app.include_router(auth.router, prefix="/api")
app.include_router(wb.router, prefix="/api")
app.include_router(ab_tests.router, prefix="/api")
app.include_router(admin.router, prefix="/api")

@app.get("/media/{file_path:path}", include_in_schema=False)
async def get_media(
    file_path: str,
    expires: int = Query(gt=0),
    signature: str = Query(min_length=64, max_length=64),
):
    """Serve uploaded previews through a short-lived, non-guessable URL."""
    if expires < int(time.time()):
        raise HTTPException(status_code=404, detail="Ссылка на изображение устарела")
    secret = (settings.media_signing_secret.strip() or settings.jwt_secret_key).encode()
    expected = hmac.new(secret, f"{file_path}:{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=404, detail="Изображение не найдено")
    target = (MEDIA_ROOT / file_path).resolve()
    try:
        target.relative_to(MEDIA_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Изображение не найдено") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Изображение не найдено")
    return FileResponse(
        target,
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": settings.app_name, "status": "running", "docs": "/docs"}
