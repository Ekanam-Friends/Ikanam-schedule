"""Единый перечень команд бота.

Из этого списка собирается и меню команд в Telegram (`setMyCommands`), и текст
`/start`. Держать их порознь — верный способ получить справку, которая обещает
команду, уже переименованную полгода назад.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BotCommandSpec:
    """Команда бота во всех местах, где она показывается пользователю."""

    name: str
    """Без ведущей косой черты."""

    menu_hint: str
    """Короткая подпись для меню Telegram: там помещается одна строка."""

    explanation: str
    """Что команда делает — эту строку человек читает в `/start`."""

    def __post_init__(self) -> None:
        # Telegram молча отклонит весь список команд, если хоть одна не подходит
        # по формату, поэтому проверяем здесь, а не выясняем это на проде.
        if not self.name.islower() or not self.name.replace("_", "").isalnum():
            raise ValueError(f"Недопустимое имя команды: {self.name!r}")
        if len(self.menu_hint) > 256:
            raise ValueError(f"Слишком длинная подпись для команды /{self.name}")


COMMANDS: tuple[BotCommandSpec, ...] = (
    BotCommandSpec(
        name="start",
        menu_hint="Что умеет бот и как он работает",
        explanation="эта справка: список команд и как устроена работа бота",
    ),
    BotCommandSpec(
        name="login",
        menu_hint="Подключить личный кабинет РАНХиГС",
        explanation="подключить личный кабинет, чтобы бот видел ваше расписание",
    ),
    BotCommandSpec(
        name="today",
        menu_hint="Пары на сегодня",
        explanation="пары на сегодня — время, аудитория, преподаватель",
    ),
    BotCommandSpec(
        name="tomorrow",
        menu_hint="Пары на завтра",
        explanation="то же самое, но на завтра",
    ),
    BotCommandSpec(
        name="week",
        menu_hint="Расписание на неделю",
        explanation="расписание на неделю вперёд",
    ),
    BotCommandSpec(
        name="calendar",
        menu_hint="Календарь: файл и ссылка-подписка",
        explanation=(
            "добавить расписание в календарь телефона — файлом или по ссылке, "
            "которая обновляется сама"
        ),
    ),
    BotCommandSpec(
        name="settings",
        menu_hint="Уведомления и напоминания",
        explanation="утренняя сводка, напоминания о парах, уведомления об изменениях",
    ),
    BotCommandSpec(
        name="status",
        menu_hint="Когда расписание обновлялось",
        explanation="когда бот в последний раз успешно забирал расписание",
    ),
    BotCommandSpec(
        name="logout",
        menu_hint="Отключить аккаунт и удалить данные",
        explanation="отключить личный кабинет и удалить всё, что бот о вас хранит",
    ),
)


def command_by_name(name: str) -> BotCommandSpec | None:
    return next((command for command in COMMANDS if command.name == name), None)
