from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.ab_test_controller import ABTestController
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.ab_test import ABTestStatus
from app.models.user import User
from app.schemas.ab_test import (
    ABTestCardResponse,
    ABTestCardsPageResponse,
    ABTestCreateRequest,
    ABTestImagePreviewResponse,
    ABTestListResponse,
    ABTestResponse,
    ABTestStartRequest,
    ABTestVariantSourceRequest,
)
from app.services.ab_test_service import ABTestService


router = APIRouter(prefix="/ab-tests", tags=["A/B-тесты"])
controller = ABTestController()


@router.get("/cards", response_model=ABTestCardsPageResponse)
async def get_cards(
    connection_id: int = Query(gt=0),
    search: str = Query(default="", max_length=120),
    cursor_updated_at: str | None = Query(default=None, max_length=64),
    cursor_nm_id: int | None = Query(default=None, gt=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.cards(db, current_user.id, connection_id, search, cursor_updated_at, cursor_nm_id)


@router.get("/cards/{nm_id}", response_model=ABTestCardResponse)
async def get_card(
    nm_id: int,
    connection_id: int = Query(gt=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.card(db, current_user.id, connection_id, nm_id)


@router.get("", response_model=ABTestListResponse)
async def get_tests(
    connection_id: int | None = Query(default=None, gt=0),
    status_value: ABTestStatus | None = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await controller.list(db, current_user.id, connection_id, status_value)
    return {"items": [ABTestService.response(item) for item in items], "total": len(items)}


@router.post("", response_model=ABTestResponse, status_code=status.HTTP_201_CREATED)
async def create_test(
    request: ABTestCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(await controller.create(db, current_user.id, request))


@router.post("/preview-image", response_model=ABTestImagePreviewResponse)
async def preview_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.preview_image(db, current_user.id, file)


@router.get("/{test_id}", response_model=ABTestResponse)
async def get_test(
    test_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(await controller.get(db, current_user.id, test_id))


@router.delete("/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test(
    test_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await controller.delete(db, current_user.id, test_id)


@router.post("/{test_id}/variants/{position}", response_model=ABTestResponse)
async def upload_variant(
    test_id: int,
    position: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(await controller.upload_variant(db, current_user.id, test_id, position, file))


@router.post("/{test_id}/variants/{position}/source", response_model=ABTestResponse)
async def set_variant_source(
    test_id: int,
    position: int,
    request: ABTestVariantSourceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(
        await controller.set_variant_source(db, current_user.id, test_id, position, request.source_url)
    )


@router.delete("/{test_id}/variants/{position}", response_model=ABTestResponse)
async def delete_variant(
    test_id: int,
    position: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(await controller.delete_variant(db, current_user.id, test_id, position))


@router.post("/{test_id}/start", response_model=ABTestResponse)
async def start_test(
    test_id: int,
    request: ABTestStartRequest = ABTestStartRequest(),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(await controller.start(db, current_user.id, test_id, request, idempotency_key))


@router.post("/{test_id}/stop", response_model=ABTestResponse)
async def stop_test(
    test_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(await controller.stop(db, current_user.id, test_id))


@router.post("/{test_id}/sync", response_model=ABTestResponse)
async def sync_test(
    test_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ABTestService.response(await controller.sync(db, current_user.id, test_id))


@router.post("/{test_id}/reconcile", response_model=ABTestResponse)
async def reconcile_test(
    test_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-read campaign/media state after an interrupted external operation."""
    return ABTestService.response(await controller.reconcile(db, current_user.id, test_id))
