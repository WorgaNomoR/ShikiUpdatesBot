import time

import pytest

import healthcheck


# ─────────────────────────────────────────────────────────────
#  Модуль healthcheck хранит состояние в глобалах
#  (_last_healthy_ts, _has_beaten, _health_threshold).
#  Сбрасываем их перед каждым тестом, чтобы тесты не влияли друг
#  на друга и не зависели от порядка запуска.
# ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_healthcheck_state():
    healthcheck._last_healthy_ts = 0.0
    healthcheck._has_beaten = False
    healthcheck._health_threshold = 45 * 60
    yield
    # Возвращаем дефолты и после теста — на всякий случай
    healthcheck._last_healthy_ts = 0.0
    healthcheck._has_beaten = False
    healthcheck._health_threshold = 45 * 60


# ============================================================
#  Пульс и определение «жив / не жив»
# ============================================================

def test_grace_period_before_first_heartbeat():
    """До первого пульса бот считается живым (грейс-период старта)."""
    assert healthcheck._has_beaten is False
    assert healthcheck._seconds_since_heartbeat() is None
    assert healthcheck._is_healthy() is True


def test_heartbeat_sets_flag_and_timestamp():
    """heartbeat() помечает пульс и обновляет время."""
    healthcheck.heartbeat()
    assert healthcheck._has_beaten is True
    elapsed = healthcheck._seconds_since_heartbeat()
    assert elapsed is not None
    assert elapsed < 1.0


def test_fresh_heartbeat_is_healthy():
    """Свежий пульс — бот жив."""
    healthcheck.heartbeat()
    assert healthcheck._is_healthy() is True


def test_stale_heartbeat_is_unhealthy():
    """Если с пульса прошло больше порога — бот не жив."""
    healthcheck._health_threshold = 10
    healthcheck.heartbeat()
    # Симулируем «давно» сдвигом метки в прошлое (а не sleep — быстрее и надёжнее)
    healthcheck._last_healthy_ts = time.monotonic() - 15
    assert healthcheck._is_healthy() is False


def test_heartbeat_within_threshold_is_healthy():
    """Пульс был, но в пределах порога — бот жив."""
    healthcheck._health_threshold = 10
    healthcheck.heartbeat()
    healthcheck._last_healthy_ts = time.monotonic() - 5
    assert healthcheck._is_healthy() is True


def test_seconds_since_heartbeat_none_without_beat():
    """Без пульса возвращается None, а не 0."""
    assert healthcheck._seconds_since_heartbeat() is None


# ============================================================
#  Порог живости из start_health_server
# ============================================================

@pytest.mark.asyncio
async def test_threshold_computed_from_interval(monkeypatch):
    """Порог = check_interval * misses; сервер реально не поднимаем."""
    started = {}

    # Подменяем web.TCPSite.start, чтобы не открывать настоящий порт
    class FakeSite:
        def __init__(self, runner, host, port):
            started["port"] = port

        async def start(self):
            started["called"] = True

    class FakeRunner:
        def __init__(self, app):
            pass

        async def setup(self):
            pass

        async def cleanup(self):
            pass

    monkeypatch.setattr(healthcheck.web, "AppRunner", FakeRunner)
    monkeypatch.setattr(healthcheck.web, "TCPSite", FakeSite)

    await healthcheck.start_health_server(check_interval=900, misses=3, port=12345)

    assert healthcheck._health_threshold == 2700  # 900 * 3
    assert started.get("called") is True
    assert started.get("port") == 12345


@pytest.mark.asyncio
async def test_threshold_default_misses(monkeypatch):
    """По умолчанию misses=3."""
    class FakeSite:
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            pass

    class FakeRunner:
        def __init__(self, app):
            pass

        async def setup(self):
            pass

    monkeypatch.setattr(healthcheck.web, "AppRunner", FakeRunner)
    monkeypatch.setattr(healthcheck.web, "TCPSite", FakeSite)

    await healthcheck.start_health_server(check_interval=600, port=12346)
    assert healthcheck._health_threshold == 1800  # 600 * 3


@pytest.mark.asyncio
async def test_port_read_from_env_when_none(monkeypatch):
    """port=None → берётся из переменной окружения PORT."""
    used = {}

    class FakeSite:
        def __init__(self, runner, host, port):
            used["port"] = port

        async def start(self):
            pass

    class FakeRunner:
        def __init__(self, app):
            pass

        async def setup(self):
            pass

    monkeypatch.setattr(healthcheck.web, "AppRunner", FakeRunner)
    monkeypatch.setattr(healthcheck.web, "TCPSite", FakeSite)
    monkeypatch.setenv("PORT", "9999")

    await healthcheck.start_health_server(check_interval=900, port=None)
    assert used.get("port") == 9999


@pytest.mark.asyncio
async def test_invalid_env_port_falls_back_to_8080(monkeypatch):
    """Некорректный PORT в env → дефолт 8080, без падения."""
    used = {}

    class FakeSite:
        def __init__(self, runner, host, port):
            used["port"] = port

        async def start(self):
            pass

    class FakeRunner:
        def __init__(self, app):
            pass

        async def setup(self):
            pass

    monkeypatch.setattr(healthcheck.web, "AppRunner", FakeRunner)
    monkeypatch.setattr(healthcheck.web, "TCPSite", FakeSite)
    monkeypatch.setenv("PORT", "not-a-number")

    await healthcheck.start_health_server(check_interval=900, port=None)
    assert used.get("port") == 8080


@pytest.mark.asyncio
async def test_start_server_failure_returns_none(monkeypatch):
    """Если сервер не поднялся — возвращаем None, не роняем бот."""
    class FakeRunner:
        def __init__(self, app):
            pass

        async def setup(self):
            raise OSError("port busy")

    monkeypatch.setattr(healthcheck.web, "AppRunner", FakeRunner)

    result = await healthcheck.start_health_server(check_interval=900, port=12347)
    assert result is None


# ============================================================
#  HTTP-эндпоинты
# ============================================================

@pytest.mark.asyncio
async def test_health_endpoint_returns_200_when_healthy():
    """GET /health → 200, пока пульс свежий (или грейс-период)."""
    resp = await healthcheck._handle_health(request=None)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_health_endpoint_returns_200_after_heartbeat():
    healthcheck.heartbeat()
    resp = await healthcheck._handle_health(request=None)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_health_endpoint_returns_503_when_stale():
    """GET /health → 503, если пульс протух."""
    healthcheck._health_threshold = 10
    healthcheck.heartbeat()
    healthcheck._last_healthy_ts = time.monotonic() - 100
    resp = await healthcheck._handle_health(request=None)
    assert resp.status == 503


@pytest.mark.asyncio
async def test_root_endpoint_returns_200():
    """GET / всегда отдаёт 200 — признак открытого порта."""
    resp = await healthcheck._handle_root(request=None)
    assert resp.status == 200
