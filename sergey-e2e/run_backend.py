"""Запуск настоящего backend WB Optimizer против поддельного WB.

Единственное вмешательство в продукт — проверка «адрес картинки не во внутренней
сети» разрешает ровно один адрес поддельного CDN (127.0.0.1:18901). Иначе
приложение не сможет скачать фото с поддельной карточки. Бизнес-логика не меняется.
"""
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.environ.get("ABTEST_SRC", "."), "backend"))

from app.services import wb_content_client as wcc  # noqa: E402

_original_check = wcc.WBContentClient._assert_public_image_url


async def _allow_mock_cdn(url: str):
    parts = urlsplit(str(url))
    if parts.hostname == "127.0.0.1" and parts.port == 18901 and parts.path.startswith("/cdn/"):
        return ["127.0.0.1"]  # новая версия закрепляет проверенный IP (защита от DNS-rebinding)
    return await _original_check(url)


wcc.WBContentClient._assert_public_image_url = staticmethod(_allow_mock_cdn)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=18900, log_level="info")
