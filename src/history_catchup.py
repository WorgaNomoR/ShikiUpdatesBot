# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистая машина связного обхода изменяемых страниц истории."""

from copy import deepcopy


def new_acquisition() -> dict:
    """Незавершённый сбор ещё не является принятой историей."""
    return {
        "phase": "tail", "page": 1, "frontier": [], "head_ids": [],
        "staged": [], "spanning": False,
    }


def resume_acquisition(state: dict) -> dict:
    """Повторно прочитать предыдущую страницу: offset мог сдвинуться."""
    state = deepcopy(state)
    state["spanning"] = state["spanning"] or bool(state["frontier"] or state["head_ids"] or state["staged"])
    state["page"] = max(1, state["page"] - 1)
    return state


def advance_acquisition(state: dict, ids: list[int], seen: set[int], limit: int) -> tuple[bool, bool]:
    """Вернуть (связная страница, полный батч), изменяя только cursor state."""
    unique = list(dict.fromkeys(ids))
    frontier = state["frontier"]
    connected = not frontier or frontier[-1] in unique
    if connected and frontier:
        # Порядок source-строк проверяется по равенству ID, не по их величине.
        common = set(frontier) & set(unique)
        connected = (
            [value for value in frontier if value in common]
            == [value for value in unique if value in common]
        )
    if not connected:
        if len(ids) < limit + 1:
            # Конец несвязного поиска не доказывает полноту. Новый проход
            # сохранит payload, но заново построит цепочку от первой страницы.
            state["page"] = 1
            state["frontier"] = []
            if state["phase"] == "tail":
                state["head_ids"] = []
        else:
            state["page"] += 1
        return False, False

    if state["phase"] == "tail" and not frontier:
        state["head_ids"] = unique
    continuation = unique[unique.index(frontier[-1]):] if frontier else unique
    boundary = set(continuation) & seen
    if state["phase"] == "head":
        boundary |= set(continuation) & set(state["head_ids"])
    complete = bool(boundary) or len(ids) < limit + 1
    if complete and state["phase"] == "tail" and state["spanning"]:
        # Старый префикс соединяется с актуальным началом выдачи отдельно.
        state["phase"] = "head"
        state["page"] = 1
        state["frontier"] = []
        return True, False
    if complete:
        return True, True
    state["frontier"] = unique
    state["page"] += 1
    return True, False
