from fastapi import APIRouter


router = APIRouter(tags=["Система"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
