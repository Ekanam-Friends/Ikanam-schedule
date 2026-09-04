"""Клиент личного кабинета РАНХиГС.

Личный кабинет — это Nuxt-приложение поверх JSON API `/lk/n-api/…`, поэтому
HTML здесь не разбирается вовсе. Формат запросов снят с фронтенда кабинета:

* `POST auth/login` — `multipart/form-data` с полями `login`, `password`,
  `remember_me`; в ответе `access_token`, `refresh_token` и `exp`;
* `POST auth/refresh` — JSON с `refresh_token`, возвращает новую пару токенов;
* `GET schedule` — `uid_org`, `type=group`, повторяющиеся `filter[]` (группы) и
  `dates[]` (по одной записи на каждый запрашиваемый день).

Две особенности, из-за которых наивный клиент не работает:

1. Кабинет стоит за WAF, который отвечает `403 Forbidden` на запрос без
   правдоподобного `User-Agent`. Заголовки ниже — не украшение.
2. Расписание запрашивается перечислением дат, а не диапазоном. Глубина выборки
   ограничена не кабинетом, а нашей вежливостью к его серверу.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://my.ranepa.ru/lk/n-api/"

MAIN_ORG_UID = "7923e704-99aa-11e5-bed4-c48508aa74e4"
"""Головная организация. У филиалов свой `uid_org`, он приходит в профиле."""

API_VERSION = "1.0"
"""Значение заголовка `version`, без которого API отвечает 400
`HttpVersionException: the request requires header with version number`.

Именно `1.0`, а не то, что отдаёт эндпоинт `version` (`currentVersion: 1.7`):
фронтенд кабинета читает из его ответа поле `version`, которого там нет, и
всегда шлёт запасное значение. Повторяем поведение фронтенда, а не документацию."""

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://my.ranepa.ru",
    "Referer": "https://my.ranepa.ru/lk/student/schedule",
    "version": API_VERSION,
    "X-User-Timezone": "+03:00",
}


class RanepaError(Exception):
    """Базовая ошибка обращения к личному кабинету."""


class AuthError(RanepaError):
    """Логин с паролем не подошли, либо токен больше не действует.

    Отличается от временных ошибок тем, что повтор не поможет: нужно, чтобы
    человек вошёл заново. Ретраить такое — прямой путь к блокировке аккаунта.
    """


class BlockedError(RanepaError):
    """Запрос отклонён защитой сайта (403).

    Обычно означает, что мы выглядим как бот: пропали заголовки браузера или с
    нашего адреса пришло слишком много запросов.
    """


class TemporaryError(RanepaError):
    """Кабинет недоступен или отвечает ошибкой — имеет смысл повторить позже."""


@dataclass(frozen=True, slots=True)
class Tokens:
    """Пара токенов доступа."""

    access_token: str
    refresh_token: str
    expires_at: datetime | None = None
    fszet: str | None = None
    """Второй секрет из ответа на вход. Фронтенд кладёт его в localStorage и
    отправляет заголовком `fszet` при каждом `auth/refresh`; без него кабинет
    отвечает 400. Что это, кабинет не объясняет, — повторяем поведение."""

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        # Минута форы: токен, истекающий через несколько секунд, всё равно не
        # переживёт медленный запрос расписания.
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(minutes=1)


class RanepaClient:
    """Асинхронный клиент личного кабинета.

    Экземпляр обслуживает одного пользователя: в нём живут его токены.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        tokens: Tokens | None = None,
    ) -> None:
        # Таймаут заметно больше обычного: кабинет регулярно отвечает на
        # `schedule` десятками секунд, и обрывать такой запрос — значит просто
        # заставить сервер сделать ту же работу ещё раз.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=BROWSER_HEADERS,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            # Прокси из окружения (HTTPS_PROXY) не трогаем принципиально. Он
            # нужен для Telegram, который из России заблокирован, — а кабинет,
            # наоборот, требует российский адрес и показывает капчу зарубежному.
            # Пропустить его через тот же прокси значит сломать вход.
            trust_env=False,
        )
        self.tokens = tokens

    async def __aenter__(self) -> RanepaClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Авторизация ---

    async def login(self, login: str, password: str, *, remember_me: bool = True) -> Tokens:
        """Войти по логину и паролю."""
        response = await self._request(
            "POST",
            "auth/login",
            files={
                "login": (None, login),
                "password": (None, password),
                "remember_me": (None, str(remember_me).lower()),
            },
        )
        self.tokens = _tokens_from_payload(response)
        return self.tokens

    async def refresh(self) -> Tokens:
        """Продлить доступ по refresh-токену, не спрашивая пароль."""
        if self.tokens is None:
            raise AuthError("Нет токенов: сначала нужно войти")

        headers = {"fszet": self.tokens.fszet} if self.tokens.fszet else {}
        response = await self._request(
            "POST",
            "auth/refresh",
            json={"refresh_token": self.tokens.refresh_token},
            headers=headers,
        )
        refreshed = _tokens_from_payload(response)
        if refreshed.fszet is None:
            # Ответ на refresh новый fszet не содержит — фронтенд после
            # обновления его не перезаписывает. Несём прежний дальше.
            refreshed = Tokens(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token,
                expires_at=refreshed.expires_at,
                fszet=self.tokens.fszet,
            )
        self.tokens = refreshed
        return self.tokens

    async def ensure_access(self) -> None:
        """Обновить токен, если срок вышел."""
        if self.tokens is not None and self.tokens.is_expired:
            await self.refresh()

    # --- Данные ---

    async def get_student_groups(self) -> dict[str, Any]:
        """Учебные данные студента: организация, группа, список групп для расписания.

        Метода для `profile` здесь нет намеренно. Тот эндпоинт отдаёт полную
        анкету — СНИЛС, дату рождения, адреса, — а всё, что нужно нам
        (`uid_student`, `uid_org`, группы), есть в `manual/student-groups`.
        Данных, которые мы не запрашиваем, не окажется ни в логах, ни в базе.
        """
        return await self._request("GET", "manual/student-groups", authorized=True)

    async def get_schedule(
        self,
        days: list[date],
        group_uids: list[str],
        *,
        org_uid: str = MAIN_ORG_UID,
        branch_type: str = "main",
    ) -> dict[str, Any]:
        """Забрать расписание за перечисленные дни.

        Args:
            days: дни, которые нужно получить. Кабинет не понимает диапазон —
                каждый день передаётся отдельным значением `dates[]`.
            group_uids: группы студента; их выдаёт профиль.
        """
        if not days:
            raise ValueError("Список дней пуст")
        if not group_uids:
            raise ValueError("Не указаны группы студента")

        params: list[tuple[str, str]] = [
            ("uid_org", org_uid),
            ("type", "group"),
            ("branch_type", branch_type),
        ]
        params += [("filter[]", uid) for uid in group_uids]
        params += [("dates[]", f"{day.isoformat()}T00:00:00.000") for day in days]

        return await self._request("GET", "schedule", params=params, authorized=True)

    # --- Транспорт ---

    async def _request(
        self,
        method: str,
        url: str,
        *,
        authorized: bool = False,
        attempts: int = 4,
        **kwargs: Any,
    ) -> Any:
        # Заголовки вызова (например, `fszet` у refresh) и авторизация — в одном
        # словаре: httpx принимает `headers` только один раз.
        headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        if authorized:
            if self.tokens is None:
                raise AuthError("Нет токена доступа")
            headers["Authorization"] = f"Bearer {self.tokens.access_token}"

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = TemporaryError(f"Кабинет не ответил вовремя: {exc}")
            except httpx.HTTPError as exc:
                last_error = TemporaryError(f"Сетевая ошибка: {exc}")
            else:
                try:
                    return self._handle(response)
                except TemporaryError as exc:
                    # Пятисотки кабинета — такой же повод повторить, как обрыв
                    # связи. Ошибки авторизации и блокировки сюда не попадают:
                    # они наследуются от других классов и уходят наверх сразу.
                    last_error = exc

            # Пауза растёт: 2, 4, 8 секунд. Кабинет «моргает» короткими 503
            # (наблюдалось вживую: три быстрых повтора за пять секунд пришлись
            # на один и тот же сбой), а долбить его чаще смысла нет — он и так
            # отвечает медленно именно тогда, когда ему тяжело.
            if attempt < attempts - 1:
                await asyncio.sleep(2 ** (attempt + 1))

        raise last_error or TemporaryError("Запрос не удался")

    def _handle(self, response: httpx.Response) -> Any:
        if response.status_code in (401, 403):
            # Оба кода приходят от разных сторон: 401 — от самого кабинета,
            # 403 без JSON — обычно от WAF, и лечится это по-разному.
            if _looks_like_waf(response):
                raise BlockedError("Запрос отклонён защитой сайта")
            # Кабинет объясняет отказ по-русски внутри JSON («Неверный логин
            # или пароль.») — эту фразу и показываем, она точнее нашей.
            raise AuthError(_cabinet_message(response) or "Кабинет не принял учётные данные")

        if response.status_code >= 500:
            raise TemporaryError(f"Кабинет ответил {response.status_code}")

        if response.status_code >= 400:
            # Путь и причина из JSON — иначе в /status видно только «400», и
            # понять, какой из четырёх запросов не понравился кабинету, нельзя.
            path = response.request.url.path.rsplit("n-api/", 1)[-1]
            reason = _cabinet_message(response) or "без объяснения"
            raise RanepaError(f"{path}: {response.status_code}, {reason}")

        try:
            return response.json()
        except ValueError as exc:
            raise TemporaryError(f"Кабинет вернул не JSON: {exc}") from exc


def _cabinet_message(response: httpx.Response) -> str | None:
    """Человекочитаемая причина отказа из JSON кабинета, если она там есть.

    Ответ вложенный: `{"message": "Unauthorized", "data": {"message": "Неверный
    логин или пароль."}}` — полезная фраза во втором слое.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    inner = payload.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("message"), str):
        return inner["message"].strip() or None
    if isinstance(payload.get("message"), str):
        return payload["message"].strip() or None
    return None


def _looks_like_waf(response: httpx.Response) -> bool:
    if response.status_code != 403:
        return False
    content_type = response.headers.get("content-type", "")
    return "json" not in content_type.lower()


def _tokens_from_payload(payload: Any) -> Tokens:
    if not isinstance(payload, dict):
        raise AuthError("Кабинет вернул неожиданный ответ на вход")

    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not access or not refresh:
        # Сюда же попадают ответы вида {"message": "Неверный логин или пароль"}.
        message = payload.get("message") or payload.get("error") or "нет токенов в ответе"
        raise AuthError(f"Вход не выполнен: {message}")

    expires_at = _parse_expiration(payload.get("exp"))
    fszet = payload.get("fszet")
    return Tokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        fszet=fszet if isinstance(fszet, str) and fszet else None,
    )


def _parse_expiration(raw: Any) -> datetime | None:
    """Разобрать срок жизни токена.

    Кабинет отдаёт `exp` числом. Значение приходит в миллисекундах, но в секундах
    оно тоже осмысленно, поэтому величина различается по порядку, а не по типу.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None

    if value > 1e11:
        value /= 1000
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
