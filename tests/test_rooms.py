"""Проверки человеческого вида аудитории — на строках, снятых с живого кабинета."""

from __future__ import annotations

import pytest

from app.ranepa.rooms import pretty_building, pretty_location, pretty_room


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 - 3406 (26) П+ПК Блок 3G", "ауд. 3406, блок 3G"),
        ("1 - 3344 (32) П+ПК Блок 3C", "ауд. 3344, блок 3C"),
        ("5 - 122 (22) П+ПК", "ауд. 122"),
        ("5 - 102б (18) ПК", "ауд. 102б"),
        ("5 - 407 (25) П+ПК", "ауд. 407"),
        ("5 - 323 (22) ПК", "ауд. 323"),
        ("Спортивный зал", "Спортивный зал"),
        ("Актовый зал (300)", "Актовый зал"),
        ("", None),
        (None, None),
    ],
)
def test_pretty_room(raw, expected):
    assert pretty_room(raw) == expected


def test_equipment_code_inside_a_word_is_not_stripped():
    """«ПК» внутри названия — не код оборудования."""
    assert pretty_room("Лаборатория ПКМ") == "Лаборатория ПКМ"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Вернадского, 82 - корпус 5", "корпус 5, Вернадского 82"),
        ("Вернадского, 84 - корпус 1", "корпус 1, Вернадского 84"),
        ("Пречистенская наб., 11", "Пречистенская наб., 11"),
        (None, None),
    ],
)
def test_pretty_building(raw, expected):
    assert pretty_building(raw) == expected


def test_location_joins_room_and_building():
    assert (
        pretty_location("1 - 3406 (26) П+ПК Блок 3G", "Вернадского, 84 - корпус 1")
        == "ауд. 3406, блок 3G, корпус 1, Вернадского 84"
    )


def test_location_without_building():
    assert pretty_location("Спортивный зал", None) == "Спортивный зал"
    assert pretty_location(None, None) == ""
