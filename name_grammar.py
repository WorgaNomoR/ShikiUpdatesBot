# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Грамматика русского имени для шаблонов сообщений, собираемая при старте."""

import logging
import re
from dataclasses import dataclass
from html import escape
from typing import Literal

from pytrovich.detector import PetrovichGenderDetector
from pytrovich.enums import (
    Case,
    Gender,
    NamePart,
)
from pytrovich.maker import PetrovichDeclinationMaker

DisplayGender = Literal["male", "female"]

_ELIGIBLE_FIRST_NAME = re.compile(
    r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*\Z"
)
_GENDER_MODES = {"auto", "male", "female", "none"}
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DisplayNameContext:
    """Сырые формы отображаемого имени, выбранные один раз при импорте."""

    nominative: str
    genitive: str
    dative: str
    accusative: str
    instrumental: str
    gender: DisplayGender | None
    inflection_applied: bool


@dataclass(frozen=True)
class GenderAlternative:
    """Значение стандартного поля ``str.format`` вида ``{g:муж|жен}``."""

    gender: DisplayGender | None

    def __format__(self, format_spec: str) -> str:
        parts = format_spec.split("|")
        if len(parts) != 2 or not all(parts):
            raise ValueError(
                "Гендерная альтернатива должна содержать два непустых "
                "варианта: {g:мужской|женский}"
            )
        return parts[1] if self.gender == "female" else parts[0]


def is_eligible_first_name(name: str) -> bool:
    """Проверяет, состоит ли имя из русских слов, разделённых дефисами."""
    return bool(_ELIGIBLE_FIRST_NAME.fullmatch(name))


def _fallback_context(name: str) -> DisplayNameContext:
    return DisplayNameContext(
        nominative=name,
        genitive=name,
        dative=name,
        accusative=name,
        instrumental=name,
        gender=None,
        inflection_applied=False,
    )


def _preserve_component_casing(original_name: str, inflected_name: str) -> str:
    """Восстанавливает нижний/верхний регистр каждой части составного имени."""
    original_parts = original_name.split("-")
    inflected_parts = inflected_name.split("-")
    if len(original_parts) != len(inflected_parts):
        return inflected_name

    result = []
    for original, inflected in zip(original_parts, inflected_parts):
        if original.isupper():
            result.append(inflected.upper())
        elif original.islower():
            result.append(inflected.lower())
        else:
            result.append(inflected)
    return "-".join(result)


def build_display_name_context(
    raw_name: str,
    gender_mode: str = "auto",
    *,
    detector_factory=PetrovichGenderDetector,
    maker_factory=PetrovichDeclinationMaker,
    logger: logging.Logger = _log,
) -> DisplayNameContext:
    """Проверяет отображаемое имя, определяет пол и склоняет его.

    Любой сбой данных или зависимости остаётся внутри функции: некорректное
    имя или сломанные правила не должны помешать запуску бота.
    """
    name = (raw_name or "").strip()
    mode = (gender_mode or "auto").strip().lower()
    fallback = _fallback_context(name)

    if mode not in _GENDER_MODES:
        logger.warning("Неподдерживаемый DISPLAY_NAME_GENDER; грамматика имени отключена")
        return fallback
    if mode == "none":
        return fallback
    if not is_eligible_first_name(name):
        logger.info(
            "DISPLAY_NAME не является поддерживаемым русским именем; "
            "грамматика имени отключена"
        )
        return fallback

    try:
        known_gender = mode != "auto"
        if mode == "auto":
            detected = detector_factory().detect(firstname=name)
            if detected == Gender.MALE:
                selected_gender: DisplayGender = "male"
            elif detected == Gender.FEMALE:
                selected_gender = "female"
            else:
                logger.info(
                    "Пол DISPLAY_NAME неоднозначен; грамматика имени отключена"
                )
                return fallback
        else:
            if mode == "male":
                selected_gender = "male"
                detected = Gender.MALE
            else:
                selected_gender = "female"
                detected = Gender.FEMALE

        maker = maker_factory()
        case_values = {
            "genitive": maker.make(
                NamePart.FIRSTNAME,
                detected,
                Case.GENITIVE,
                name,
                known_gender=known_gender,
            ),
            "dative": maker.make(
                NamePart.FIRSTNAME,
                detected,
                Case.DATIVE,
                name,
                known_gender=known_gender,
            ),
            "accusative": maker.make(
                NamePart.FIRSTNAME,
                detected,
                Case.ACCUSATIVE,
                name,
                known_gender=known_gender,
            ),
            "instrumental": maker.make(
                NamePart.FIRSTNAME,
                detected,
                Case.INSTRUMENTAL,
                name,
                known_gender=known_gender,
            ),
        }
        if any(not isinstance(value, str) or not value for value in case_values.values()):
            raise ValueError("pytrovich вернул пустую или нестроковую форму имени")
        case_values = {
            case_name: _preserve_component_casing(name, value)
            for case_name, value in case_values.items()
        }

        return DisplayNameContext(
            nominative=name,
            gender=selected_gender,
            inflection_applied=True,
            **case_values,
        )
    except Exception:
        logger.warning(
            "Не удалось собрать грамматику DISPLAY_NAME; используем исходное имя",
            exc_info=True,
        )
        return fallback


def template_values(context: DisplayNameContext) -> dict[str, object]:
    """Возвращает HTML-безопасные формы имени и гендерный форматтер."""
    return {
        "n": escape(context.nominative),
        "n_gen": escape(context.genitive),
        "n_dat": escape(context.dative),
        "n_acc": escape(context.accusative),
        "n_ins": escape(context.instrumental),
        "g": GenderAlternative(context.gender),
    }


def format_name_template(
    template: str,
    context: DisplayNameContext,
    **values: object,
) -> str:
    """Подставляет в шаблон независимые HTML-безопасные формы имени."""
    fields = template_values(context)
    fields.update(values)
    return template.format(**fields)
