"""Настройки приложения.

Все секреты читаются из окружения — в репозитории лежит только `.env.example`
с пустыми значениями. Приложение падает на старте, если чего-то не хватает:
лучше не подняться совсем, чем работать половиной функций и молча не отдавать
пользователям расписание.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: SecretStr = Field(alias="BOT_TOKEN")
    owner_chat_id: int | None = Field(default=None, alias="OWNER_CHAT_ID")
    """Куда бот пишет, что синхронизация сломалась. Без него о поломке узнают
    только пользователи, а это ровно тот сценарий, который убивает такие проекты."""

    telegram_proxy: str | None = Field(default=None, alias="TELEGRAM_PROXY")
    """Прокси до api.telegram.org, например `socks5://user:pass@host:1080`.

    Нужен там, где Telegram недоступен напрямую. Кабинет РАНХиГС при этом
    требует российский адрес, так что прокси касается только Telegram —
    запросы к кабинету через него не идут."""

    # --- Шифрование учётных данных пользователей ---
    credentials_key: SecretStr = Field(alias="CREDENTIALS_KEY")

    # --- База ---
    database_url: str = Field(alias="DATABASE_URL")

    # --- Публичный ICS-фид ---
    public_base_url: str = Field(alias="PUBLIC_BASE_URL")
    """Внешний адрес сервиса. Из него собираются ссылки-подписки, поэтому он
    обязан быть тем, что видит календарь пользователя, а не localhost."""

    # --- Источник ---
    ranepa_base_url: str = Field(default="https://my.ranepa.ru", alias="RANEPA_BASE_URL")
    sync_hour_msk: int = Field(default=3, ge=0, le=23, alias="SYNC_HOUR_MSK")
    token_max_age_days: int = Field(default=30, ge=1, le=365, alias="TOKEN_MAX_AGE_DAYS")
    """Сколько дней без удачной синхронизации считать ключ доступа живым.

    Настоящий срок refresh-токена задаёт кабинет, и нам он неизвестен. Это
    наша граница доверия: если месяц подряд ключ не удаётся обновить, дальше
    ходить с ним в чужой кабинет бессмысленно — просим человека войти заново."""

    ranepa_challenge_solver: Literal["none", "playwright"] = Field(
        default="none", alias="RANEPA_CHALLENGE_SOLVER"
    )
    """Как проходить JS-проверку антибота кабинета.

    С адресов дата-центров кабинет отдаёт вместо API страницу, которая
    считает хеш в браузере и ставит cookie. `playwright` — пройти её настоящим
    headless Chromium (входит в docker-образ) и ходить в API с полученными
    cookie. `none` — не проходить: годится там, где проверки нет, например с
    домашнего адреса; иначе вход будет падать с понятной ошибкой."""

    sync_concurrency: int = Field(default=4, ge=1, le=32, alias="SYNC_CONCURRENCY")
    """Сколько аккаунтов синхронизируются одновременно. Держим низким осознанно:
    несколько сотен логинов с одного адреса — заметная нагрузка на чужой сервер,
    и лишний повод для его администраторов заблокировать наш IP."""

    @field_validator("public_base_url", "ranepa_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("owner_chat_id", mode="before")
    @classmethod
    def _empty_means_absent(cls, value: object) -> object:
        """Пустая строка в `.env` — это «не задано», а не ошибка.

        Необязательные переменные в `.env` принято оставлять пустыми, а не
        удалять строку целиком; без этой обработки приложение не поднимется.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ranepa_challenge_solver", mode="before")
    @classmethod
    def _solver_name(cls, value: object) -> object:
        """Пустое значение — «не задано», регистр не важен."""
        if isinstance(value, str):
            value = value.strip().lower()
            return value or "none"
        return value

    def feed_url(self, token: str) -> str:
        """Ссылка на персональный ICS-фид."""
        return f"{self.public_base_url}/feed/{token}.ics"

    def webcal_url(self, token: str) -> str:
        """Та же ссылка со схемой `webcal://`.

        iOS и macOS по такой ссылке сразу предлагают добавить подписку, тогда как
        `https://` они просто скачают как файл — и пользователь получит разовый
        импорт вместо подписки, сам того не заметив.
        """
        return self.feed_url(token).replace("https://", "webcal://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
