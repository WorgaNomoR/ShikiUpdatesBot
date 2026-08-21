# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты config.py — хелперы окружения и чтение env (env-config фолд)."""

import importlib
from unittest.mock import MagicMock

import pytest

import config
from config import (
    _int_env,
    _load_local_env,
    _required_env,
    _required_int_env,
)


# ── _required_env ──────────────────────────────────────────────────
def test_required_env_returns_value(monkeypatch):
    monkeypatch.setenv("X_FOO", "bar")
    assert _required_env("X_FOO") == "bar"


def test_required_env_strips_whitespace(monkeypatch):
    monkeypatch.setenv("X_FOO", "  bar  ")
    assert _required_env("X_FOO") == "bar"


def test_required_env_missing_raises(monkeypatch):
    monkeypatch.delenv("X_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="X_MISSING"):
        _required_env("X_MISSING", "подсказка")


def test_required_env_blank_raises(monkeypatch):
    monkeypatch.setenv("X_BLANK", "   ")
    with pytest.raises(RuntimeError):
        _required_env("X_BLANK")


def test_required_env_hint_in_message(monkeypatch):
    monkeypatch.delenv("X_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="подсказка"):
        _required_env("X_MISSING", "подсказка")


# ── _int_env ───────────────────────────────────────────────────────
def test_int_env_default_when_absent(monkeypatch):
    monkeypatch.delenv("X_INT", raising=False)
    assert _int_env("X_INT", 42) == 42


def test_int_env_default_when_blank(monkeypatch):
    monkeypatch.setenv("X_INT", "   ")
    assert _int_env("X_INT", 42) == 42


def test_int_env_parses_value(monkeypatch):
    monkeypatch.setenv("X_INT", "100")
    assert _int_env("X_INT", 42) == 100


def test_int_env_bad_value_raises(monkeypatch):
    monkeypatch.setenv("X_INT", "notanumber")
    with pytest.raises(RuntimeError, match="X_INT"):
        _int_env("X_INT", 42)


def test_required_int_env_names_invalid_variable_without_value(monkeypatch):
    monkeypatch.setenv("X_SECRET_INT", "not-a-number")
    with pytest.raises(RuntimeError, match="X_SECRET_INT") as error:
        _required_int_env("X_SECRET_INT")
    assert "not-a-number" not in str(error.value)


def test_load_local_env_uses_utf8_sig_without_overriding_environment(
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / ".env"
    load_dotenv = MagicMock()
    monkeypatch.setattr(config, "load_dotenv", load_dotenv)

    _load_local_env(env_file)

    load_dotenv.assert_called_once_with(
        dotenv_path=env_file,
        override=False,
        encoding="utf-8-sig",
    )


# ── чтение конфигурации из окружения (conftest задаёт значения) ─────
def test_config_reads_shiki_user_from_env():
    import config
    assert config.SHIKI_USER == "WNR"          # задан в conftest


def test_config_shiki_base_url_has_default():
    import config
    assert config.SHIKI_BASE_URL == "https://shikimori.io"


def test_config_display_name_falls_back_to_shiki_user():
    import config
    # DISPLAY_NAME не задан в conftest -> фолбэк на ник SHIKI_USER
    assert config.DISPLAY_NAME == config.SHIKI_USER


def test_config_display_name_gender_defaults_to_auto():
    import config
    assert config.DISPLAY_NAME_GENDER == "auto"


@pytest.mark.parametrize(("raw_value", "expected"), [
    (" FEMALE ", "female"),
    ("   ", "auto"),
])
def test_config_display_name_gender_normalizes_env(monkeypatch, raw_value, expected):
    import config

    monkeypatch.setenv("DISPLAY_NAME_GENDER", raw_value)
    try:
        reloaded = importlib.reload(config)
        assert reloaded.DISPLAY_NAME_GENDER == expected
    finally:
        monkeypatch.delenv("DISPLAY_NAME_GENDER", raising=False)
        importlib.reload(config)


def test_config_intervals_have_int_defaults():
    import config
    assert isinstance(config.CHECK_INTERVAL, int)
    assert config.CHECK_INTERVAL == 15 * 60


def test_config_owner_id_is_int():
    import config
    # OWNER_ID приходит из env строкой — config приводит к int
    assert isinstance(config.OWNER_ID, int)
    assert config.OWNER_ID == 123456  # задан в conftest
