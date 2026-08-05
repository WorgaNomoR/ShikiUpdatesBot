# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Версия и адреса проекта, встроенные в исходный и frozen-режимы."""

import build_info
import project_meta


def test_semver_tuple_is_strict():
    assert build_info.semver_tuple("v1.2.3") == (1, 2, 3)
    assert build_info.semver_tuple("1.2.3") == (1, 2, 3)
    assert build_info.semver_tuple("v1.2") is None
    assert build_info.semver_tuple("v01.2.3") is None
    assert build_info.semver_tuple("dev") is None


def test_source_mode_has_canonical_project_version_and_repository():
    assert build_info.APP_VERSION == project_meta.PROJECT_VERSION
    assert build_info.APP_REPOSITORY == project_meta.PROJECT_REPOSITORY
