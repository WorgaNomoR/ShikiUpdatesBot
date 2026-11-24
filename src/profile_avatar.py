# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Необязательный аватар проверенного профиля для локальных карточек."""

import asyncio
import re
import time
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from urllib.parse import (
    urljoin,
    urlsplit,
)
from weakref import WeakKeyDictionary

import aiohttp
from PIL import (
    Image,
    ImageOps,
)

from config import (
    SHIKI_BASE_URL,
    log,
)
from shiki_api import fetch_profile_avatar

_REFRESH_AFTER = 6 * 60 * 60
_RETRY_AFTER = 30 * 60
_SIZE = 160
_FORMATS = {"PNG", "JPEG", "WEBP"}


@dataclass(frozen=True)
class ProfileAvatar:
    """Нормализованное изображение с привязкой к подтверждённому ID."""

    user_id: int | None = None
    image: bytes | None = None
    digest: bytes | None = None

    @property
    def version(self) -> tuple[int | None, bytes | None]:
        """Логотип общий; загруженное изображение принадлежит одной личности."""
        return (self.user_id, self.digest) if self.image else (None, None)


@dataclass
class _AvatarState:
    source: tuple[int, str | None] | None = None
    avatar: ProfileAvatar = ProfileAvatar()
    task: asyncio.Task | None = None
    next_attempt: float = 0
    closed: bool = False


_states = WeakKeyDictionary()


def _avatar_url(value: object, user_id: int) -> str | None:
    """Разрешить HTTPS с доверенного origin и путь x160 ровно этого пользователя."""
    if not isinstance(value, str) or len(value) > 1024 or re.search(r"[\s\\]", value):
        return None
    if value.startswith("//"):
        return None
    try:
        base = urlsplit(SHIKI_BASE_URL)
        url = urljoin(SHIKI_BASE_URL, value)
        parts = urlsplit(url)
        origin = (parts.hostname, parts.port or 443)
        if (
            parts.scheme != "https" or origin != (base.hostname, base.port or 443)
            or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.fragment or not re.fullmatch(rf"/system/users/x160/{user_id}\.(png|jpe?g|webp)", parts.path)
        ):
            return None
    except ValueError:
        return None
    return url


def _normalize(content: bytes) -> bytes | None:
    """Проверить формат и размеры перед декодированием, отбросить метаданные."""
    try:
        with Image.open(BytesIO(content), formats=tuple(_FORMATS)) as image:
            if max(image.size) > 512 or getattr(image, "is_animated", False):
                return None
            pixels = ImageOps.fit(image.convert("RGBA"), (_SIZE, _SIZE), Image.Resampling.LANCZOS)
        pixels.info.clear()
        output = BytesIO()
        pixels.save(output, format="PNG")
        return output.getvalue() if pixels.getchannel("A").getbbox() else None
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        return None


async def _refresh(state: _AvatarState, source: tuple[int, str]) -> None:
    user_id, url = source
    try:
        async with aiohttp.ClientSession() as session:
            raw = await fetch_profile_avatar(session, url)
        image = await asyncio.to_thread(_normalize, raw) if raw else None
        if state.source == source and image:
            state.avatar = ProfileAvatar(user_id, image, sha256(image).digest())
            state.next_attempt = time.monotonic() + _REFRESH_AFTER
    except Exception as error:
        log.debug("Аватар профиля: обновление пропущено (%s).", type(error).__name__)
    finally:
        state.task = None


def observe_profile_avatar(user_id: int, value: object, *, matches: bool = True) -> None:
    """Запланировать один ограниченный запрос после проверки профиля при опросе."""
    state = _states.setdefault(asyncio.get_running_loop(), _AvatarState())
    if state.closed:
        return
    url = _avatar_url(value, user_id) if matches else None
    source = user_id, url
    if source != state.source:
        state.source = source
        state.avatar = ProfileAvatar(user_id)
        state.next_attempt = 0
    now = time.monotonic()
    if url and state.task is None and now >= state.next_attempt:
        state.next_attempt = now + _RETRY_AFTER
        state.task = asyncio.create_task(_refresh(state, (user_id, url)))


def current_profile_avatar() -> ProfileAvatar:
    """Снять текущий аватар без сети или декодирования."""
    state = _states.get(asyncio.get_running_loop())
    return state.avatar if state else ProfileAvatar()


async def stop_profile_avatar() -> None:
    """Остановить сетевое обновление перед завершением приложения."""
    state = _states.setdefault(asyncio.get_running_loop(), _AvatarState())
    state.closed = True
    if state.task is not None:
        state.task.cancel()
        await asyncio.gather(state.task, return_exceptions=True)
