"""Человеческий вид аудитории.

Кабинет отдаёт строку для диспетчера, а не для студента:

    1 - 3406 (26) П+ПК Блок 3G

Здесь `1` — корпус (он же есть в адресе), `(26)` — вместимость, `П+ПК` — код
оборудования (проектор и компьютер), `Блок 3G` — крыло здания. Студенту, чтобы
дойти, нужны номер и блок; всё остальное — шум, который в узкой колонке
телефона вытесняет полезное.
"""

from __future__ import annotations

import re

_CAPACITY = re.compile(r"\(\s*\d+\s*\)")
_EQUIPMENT = re.compile(r"(?<![\wА-Яа-я])(?:П\+ПК|ПК|П|ИД|ПК\+ИД)(?![\wА-Яа-я])")
_BLOCK = re.compile(r"[Бб]лок\s+([A-Za-zА-Яа-я0-9]+)")
_LEADING_CORPUS = re.compile(r"^\s*\d+\s*-\s*")
_SPACES = re.compile(r"\s{2,}")


def pretty_room(raw: str | None) -> str | None:
    """«1 - 3406 (26) П+ПК Блок 3G» → «ауд. 3406, блок 3G».

    Незнакомый формат («Спортивный зал», «Актовый зал») возвращается как есть:
    испортить название хуже, чем оставить длинным.
    """
    if not raw:
        return None
    text = raw.strip()

    block_match = _BLOCK.search(text)
    block = block_match.group(1) if block_match else None

    body = _LEADING_CORPUS.sub("", text)
    body = _BLOCK.sub("", body)
    body = _CAPACITY.sub("", body)
    body = _EQUIPMENT.sub("", body)
    body = _SPACES.sub(" ", body).strip(" ,")

    # Осталось что-то похожее на номер аудитории — оформляем как «ауд.».
    # Иначе это название зала, и трогать его не нужно.
    if body and re.match(r"^\d+[\wА-Яа-я/-]*$", body):
        result = f"ауд. {body}"
    else:
        result = body or text

    if block:
        result = f"{result}, блок {block}"
    return result


def pretty_building(raw: str | None) -> str | None:
    """«Вернадского, 82 - корпус 5» → «корпус 5, Вернадского 82».

    Корпус важнее улицы: улица у большинства пар одна и та же, а корпус — то,
    из-за чего опаздывают.
    """
    if not raw:
        return None
    if " - " not in raw:
        return raw.strip()
    street, _, corpus = raw.partition(" - ")
    return f"{corpus.strip()}, {street.replace(',', '').strip()}"


def pretty_location(room: str | None, building: str | None) -> str:
    """Место одной строкой: «ауд. 3406, блок 3G, корпус 1, Вернадского 84»."""
    parts = [p for p in (pretty_room(room), pretty_building(building)) if p]
    return ", ".join(parts)
