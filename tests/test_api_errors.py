import asyncio
import json

import aiohttp
import pytest

from main import (
    fetch_history,
    fetch_favourites,
)


# ============================================================
# fetch_history()
# ============================================================

@pytest.mark.asyncio
async def test_fetch_history_timeout(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    result = await fetch_history(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_history_client_error(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise aiohttp.ClientError("boom")

    result = await fetch_history(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_history_invalid_json(monkeypatch):
    class FakeResponse:
        status = 200

        async def json(self):
            raise json.JSONDecodeError("bad", "", 0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_history(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_history_bad_status(monkeypatch):
    class FakeResponse:
        status = 500

        async def json(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_history(FakeSession())

    assert result is None


# ============================================================
# fetch_favourites()
# ============================================================

@pytest.mark.asyncio
async def test_fetch_favourites_timeout(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    result = await fetch_favourites(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_favourites_client_error(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise aiohttp.ClientError("boom")

    result = await fetch_favourites(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_favourites_invalid_json(monkeypatch):
    class FakeResponse:
        status = 200

        async def json(self):
            raise json.JSONDecodeError("bad", "", 0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_favourites(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_favourites_bad_status(monkeypatch):
    class FakeResponse:
        status = 500

        async def json(self):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_favourites(FakeSession())

    assert result is None