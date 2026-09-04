from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.auth_controller import AuthController
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    EmailCodeRequest,
    EmailRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterStartRequest,
    RegisterStartResponse,
    ResetPasswordRequest,
    TokenResponse,
    UserProfileUpdateRequest,
    UserResponse,
)


router = APIRouter(prefix="/auth", tags=["Авторизация"])
controller = AuthController()


@router.post("/register/start", response_model=RegisterStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def register_start(request: RegisterStartRequest, db: AsyncSession = Depends(get_db)):
    return await controller.register(db, request.model_dump())


@router.post("/register/verify", response_model=TokenResponse)
async def register_verify(request: EmailCodeRequest, db: AsyncSession = Depends(get_db)):
    return await controller.verify_email(db, request.model_dump())


@router.post("/register/resend", response_model=RegisterStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def register_resend(request: EmailRequest, db: AsyncSession = Depends(get_db)):
    return await controller.resend_code(db, str(request.email))


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    return await controller.login(db, request.model_dump())


@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: RefreshRequest, db: AsyncSession = Depends(get_db)):
    return await controller.refresh(db, request.refresh_token)


@router.post("/logout", response_model=MessageResponse)
async def logout(request: RefreshRequest, db: AsyncSession = Depends(get_db)):
    await controller.logout(db, request.refresh_token)
    return {"message": "Вы вышли из аккаунта"}


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/profile", response_model=UserResponse)
async def update_profile(
    request: UserProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await controller.update_profile(db, current_user, request.model_dump())


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    request: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await controller.change_password(db, current_user, request.model_dump())
    return {"message": "Пароль изменён. Войдите снова."}


@router.post("/forgot-password", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(request: EmailRequest, db: AsyncSession = Depends(get_db)):
    return await controller.request_reset(db, str(request.email))


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(request: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    await controller.reset_password(db, request.model_dump())
    return {"message": "Пароль успешно сброшен. Теперь можно войти."}
