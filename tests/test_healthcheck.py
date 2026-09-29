# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
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
    healthcheck._polling_started_at = None
    healthcheck._polling_stopped = False
    healthcheck._health_threshold = 45 * 60
    yield
    # Возвращаем дефолты и после теста — на всякий случай
    healthcheck._last_healthy_ts = 0.0
    healthcheck._has_beaten = False
    healthcheck._polling_started_at = None
    healthcheck._polling_stopped = False
    healthcheck._health_threshold = 45 * 60


# ============================================================
#  Пульс и определение «жив / не жив»
# ============================================================

@pytest.fixture
def fake_web(monkeypatch):
    """Заглушки aiohttp web.AppRunner/TCPSite — не открываем реальный порт.
    Возвращает captured: port (TCPSite), started (bool), runner_kwargs (kwargs AppRunner)."""
    captured = {"port": None, "started": False, "runner_kwargs": {}}

    class FakeSite:
        def __init__(self, runner, host, port):
            captured["port"] = port

        async def start(self):
            captured["started"] = True

    class FakeRunner:
        def __init__(self, app, **kwargs):
            captured["runner_kwargs"] = kwargs

        async def setup(self):
            pass

        async def cleanup(self):
            pass

    monkeypatch.setattr(healthcheck.web, "AppRunner", FakeRunner)
    monkeypatch.setattr(healthcheck.web, "TCPSite", FakeSite)
    return captured


def test_grace_period_before_first_heartbeat():
    """До первого пульса бот считается живым (грейс-период старта).
    Это и есть защита от рестарт-петли при недоступном владельце."""
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
async def test_threshold_computed_from_interval(fake_web):
    """Порог = check_interval * misses; сервер реально не поднимаем."""
    await healthcheck.start_health_server(check_interval=900, misses=3, port=12345)

    assert healthcheck._health_threshold == 2700  # 900 * 3
    assert fake_web["started"] is True
    assert fake_web["port"] == 12345


@pytest.mark.asyncio
async def test_threshold_default_misses(fake_web):
    """По умолчанию misses=3."""
    await healthcheck.start_health_server(check_interval=600, port=12346)
    assert healthcheck._health_threshold == 1800  # 600 * 3


@pytest.mark.asyncio
async def test_threshold_guards_against_zero(fake_web):
    """check_interval/misses=0 не должны обнулить порог (max(1, ...))."""
    await healthcheck.start_health_server(check_interval=0, misses=0, port=12348)
    assert healthcheck._health_threshold == 1   # max(1,0) * max(1,0)


@pytest.mark.asyncio
async def test_port_read_from_env_when_none(fake_web, monkeypatch):
    """port=None → берётся из переменной окружения PORT."""
    monkeypatch.setenv("PORT", "9999")

    await healthcheck.start_health_server(check_interval=900, port=None)
    assert fake_web["port"] == 9999


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["not-a-number", "0", "-1", "65536"])
async def test_invalid_env_port_falls_back_to_8080(fake_web, monkeypatch, value):
    """Некорректный PORT в env → дефолт 8080, без падения."""
    monkeypatch.setenv("PORT", value)

    await healthcheck.start_health_server(check_interval=900, port=None)
    assert fake_web["port"] == 8080


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "1", "65535"])
async def test_default_and_boundary_env_ports(fake_web, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PORT", raising=False)
    else:
        monkeypatch.setenv("PORT", value)
    await healthcheck.start_health_server(check_interval=900)
    assert fake_web["port"] == (8080 if value is None else int(value))


@pytest.mark.asyncio
async def test_start_server_failure_returns_none(monkeypatch):
    """Если сервер не поднялся — возвращаем None, не роняем бот."""
    class FakeRunner:
        def __init__(self, app, **kwargs):
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


@pytest.mark.asyncio
async def test_health_server_disables_access_log(fake_web):
    """AppRunner создаётся с access_log=None — иначе каждый опрос liveness-пробы
    (раз в минуту) спамит в лог строкой GET /health 200 (access-лог aiohttp)."""
    await healthcheck.start_health_server(check_interval=900, port=12345)

    assert "access_log" in fake_web["runner_kwargs"], "AppRunner вызван без access_log"
    assert fake_web["runner_kwargs"]["access_log"] is None


@pytest.mark.asyncio
async def test_active_first_cycle_grace_is_bounded_and_restart_is_fresh(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("healthcheck.time.monotonic", lambda: now[0])
    healthcheck._health_threshold = 30
    now[0] += 10000
    assert (await healthcheck._handle_health(None)).status == 200
    healthcheck.polling_started()
    now[0] += 29.999
    assert (await healthcheck._handle_health(None)).status == 200
    now[0] += 0.001
    assert (await healthcheck._handle_health(None)).status == 503
    healthcheck.polling_started()
    assert healthcheck._seconds_since_heartbeat() is None
    assert (await healthcheck._handle_health(None)).status == 200
    healthcheck.heartbeat()
    now[0] += 30
    assert (await healthcheck._handle_health(None)).status == 503
    healthcheck.polling_started()
    assert (await healthcheck._handle_health(None)).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("beaten", [False, True])
async def test_stopped_task_is_unhealthy_even_before_first_heartbeat(beaten):
    healthcheck.polling_started()
    if beaten:
        healthcheck.heartbeat()
    healthcheck.polling_stopped()
    assert (await healthcheck._handle_health(None)).status == 503
    healthcheck.polling_started()
    assert (await healthcheck._handle_health(None)).status == 200
