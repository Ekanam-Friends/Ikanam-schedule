"""Проверки приветственного сообщения и перечня команд.

Смысл этих тестов — не в тексте как таковом, а в том, что справка не разъезжается
с реальным набором команд и что обещания про учётные данные из неё не пропадают
при правках.
"""

from __future__ import annotations

from app.bot.commands import COMMANDS, BotCommandSpec, command_by_name
from app.bot.texts import TELEGRAM_MESSAGE_LIMIT, build_start_message


def test_start_lists_every_registered_command():
    """Меню Telegram и текст /start собираются из одного списка — проверяем связь."""
    message = build_start_message()

    for command in COMMANDS:
        assert f"/{command.name}" in message, f"команда /{command.name} потерялась в /start"
        assert command.explanation in message


def test_every_command_explains_itself():
    for command in COMMANDS:
        assert command.explanation.strip()
        assert command.menu_hint.strip()


def test_start_explains_how_the_bot_works():
    message = build_start_message()

    assert "/login" in message
    assert "расписание" in message.lower()
    assert "календар" in message.lower()


def test_start_is_honest_about_credentials():
    """Пользователь отдаёт боту доступ к учебному аккаунту — он должен знать это заранее."""
    message = build_start_message()

    assert "зашифрованном" in message
    assert "неофициальный" in message
    assert "/logout" in message


def test_start_promises_exactly_what_the_code_does():
    """Обещание «пароль не храню» должно совпадать со схемой базы.

    Если из `User` когда-нибудь снова появится поле с паролем, этот тест
    напомнит, что текст в боте стал неправдой.
    """
    from app.db.models import User

    message = build_start_message()
    columns = set(User.__table__.columns.keys())

    assert "Пароль я не храню" in message
    assert not [name for name in columns if "password" in name]
    assert "refresh_token_encrypted" in columns


def test_start_hints_where_to_begin_only_when_needed():
    fresh = build_start_message(is_connected=False)
    connected = build_start_message(is_connected=True)

    assert "начните с /login" in fresh
    assert "начните с /login" not in connected


def test_start_fits_into_one_telegram_message():
    """Больше 4096 символов Telegram не примет — сообщение просто не отправится."""
    assert len(build_start_message()) < TELEGRAM_MESSAGE_LIMIT


def test_command_names_are_valid_for_telegram():
    for command in COMMANDS:
        assert command.name.islower()
        assert len(command.name) <= 32
        assert len(command.menu_hint) <= 256


def test_invalid_command_name_is_rejected_early():
    """Один неверный формат — и Telegram отклоняет весь список команд целиком."""
    try:
        BotCommandSpec(name="Today", menu_hint="x", explanation="x")
    except ValueError:
        pass
    else:
        raise AssertionError("имя с заглавной буквой должно отклоняться")


def test_lookup_by_name():
    assert command_by_name("today") is not None
    assert command_by_name("nonexistent") is None
