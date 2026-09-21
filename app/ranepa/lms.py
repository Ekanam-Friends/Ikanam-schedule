"""Клиент СДО РАНХиГС (`lms.ranepa.ru`) — отметка посещаемости по QR.

Это **не** личный кабинет из `client.py`. СДО — это Moodle на отдельном домене
со своим входом: не JSON-API с токенами, а обычная форма `/login/index.php`
(поля `username`, `password`, скрытый `logintoken`), после которой сессия живёт
в cookie `MoodleSession`. Тот же логин и пароль, что у кабинета, но вход другой,
и refresh-токен кабинета сюда не подходит.

Отметка устроена так: преподаватель показывает QR со ссылкой
`mod/attendancernhgs/qrcode.php?qr=<хеш>`. Хеш живёт секунды (наблюдалось
40–60 с) и на стороне сервера сам указывает на нужную сессию и курс — клиенту
знать про курс не нужно. Студент, уже вошедший в Moodle, открывает эту ссылку —
и Moodle засчитывает присутствие. Открытие протухшего хеша отвечает страницей
«QR код уже неактивен. Для отметки посещаемости, повторите сканирование».

Отсюда устройство клиента: держать одну тёплую сессию Moodle (вход — дорогой,
делаем его редко), а на каждую отметку — один быстрый GET. Если сессия истекла,
Moodle редиректит на форму входа — это единственный сигнал перелогиниться.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://lms.ranepa.ru"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_LOGIN_TOKEN_RE = re.compile(r'name="logintoken"\s+value="([a-zA-Z0-9]+)"')
_QR_HASH_RE = re.compile(r"qr=([a-f0-9]{16,})", re.IGNORECASE)

# Ссылка подключения к живому вебинару MTS Link: `.../event/<event>/<access>`.
# Отличается от записи прошедшего (`.../j/Ranepa/<event>/record-new/<rec>`) —
# запись нам не нужна, ловим только живую. Снято с модуля `mtslinkrnhgs`
# 2026-09-21.
_MTS_LIVE_RE = re.compile(r"https://my\.mts-link\.ru/event/\d+/\d+")

# Маркеры протухшего/недоступного QR на странице ответа. Текст снят вживую
# 2026-09-21: открытие устаревшего хеша отдаёт именно «уже неактивен».
_EXPIRED_MARKERS = ("неактив", "повторите сканирован")

# Маркеры удачной отметки. Снято вживую 2026-09-21: успех — зелёный блок
# «Успешно! … Статус: Присутствовал». Хватает любого из двух.
_SUCCESS_MARKERS = ("успешно", "статус: присутств")


class LmsError(Exception):
    """Базовая ошибка обращения к СДО."""


class LmsAuthError(LmsError):
    """Логин/пароль не подошли — повтор не поможет, нужен верный пароль."""


class LmsTemporaryError(LmsError):
    """СДО недоступна или ответила ошибкой — имеет смысл повторить позже."""


class MarkResult(Enum):
    """Исход попытки отметиться по хешу."""

    MARKED = "marked"
    """Присутствие засчитано."""

    EXPIRED = "expired"
    """Хеш протух: QR сменился, пока фото ехало до бота. Нужен свежий кадр."""

    UNKNOWN = "unknown"
    """Ответ не распознан. До первого живого успеха — сюда попадает всё, что
    не является явным «протух»: раскладку успешного ответа нужно снять вживую
    и добавить в `_classify`, пока не угадываем."""


@dataclass(frozen=True, slots=True)
class Marked:
    result: MarkResult
    message: str
    """Человекочитаемая строка со страницы Moodle — её же показываем в чат."""


def extract_qr_hash(text: str) -> str | None:
    """Достать хеш из ссылки QR или из голого значения.

    Принимает и полный URL (`…/qrcode.php?qr=abc123`), и просто `abc123`.
    Возвращает None, если это не похоже на хеш отметки РАНХиГС.
    """
    text = text.strip()
    match = _QR_HASH_RE.search(text)
    if match:
        return match.group(1).lower()
    if re.fullmatch(r"[a-f0-9]{16,}", text, re.IGNORECASE):
        return text.lower()
    return None


class LmsClient:
    """Асинхронный клиент СДО для одного пользователя.

    Держит его сессию Moodle в cookie. Экземпляр рассчитан на переиспользование:
    вход делается один раз (или заново после истечения), отметки — много.
    """

    def __init__(
        self,
        login: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._login = login
        self._password = password
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=BROWSER_HEADERS,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            # СДО требует российский адрес и не ходит через прокси для Telegram —
            # ровно как кабинет. Прокси из окружения не трогаем.
            trust_env=False,
        )
        self._logged_in = False

    async def __aenter__(self) -> LmsClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        """Войти в Moodle по логину и паролю, получить cookie сессии.

        Вход — форма в два шага: страница логина отдаёт `logintoken` и ставит
        первую cookie, затем POST с учётными данными меняет её на сессию
        вошедшего пользователя.
        """
        try:
            page = await self._client.get("/login/index.php")
        except httpx.HTTPError as exc:
            raise LmsTemporaryError(f"СДО не ответила на странице входа: {exc}") from exc

        token_match = _LOGIN_TOKEN_RE.search(page.text)
        if token_match is None:
            raise LmsTemporaryError("СДО не отдала форму входа (нет logintoken)")

        try:
            resp = await self._client.post(
                "/login/index.php",
                data={
                    "username": self._login,
                    "password": self._password,
                    "logintoken": token_match.group(1),
                },
            )
        except httpx.HTTPError as exc:
            raise LmsTemporaryError(f"СДО оборвала вход: {exc}") from exc

        # Успешный вход у Moodle редиректит на /my/; неверный пароль оставляет
        # на /login с ошибкой в теле. Отличаем по конечному адресу и по наличию
        # ссылки «Выход» — она есть только у вошедшего.
        if "/login/index.php" in str(resp.url) or "logout" not in resp.text:
            raise LmsAuthError("СДО не приняла логин или пароль")

        self._logged_in = True

    async def mark(self, qr_hash: str) -> Marked:
        """Отметиться по хешу QR. При истёкшей сессии — один перелогин и повтор."""
        if not self._logged_in:
            await self.login()

        marked = await self._open_qr(qr_hash)
        if marked is None:
            # Сессия истекла: Moodle увёл на форму входа. Заходим заново и
            # пробуем ровно один раз — если и теперь редирект, дело не в сессии.
            log.info("Сессия СДО истекла — вхожу заново")
            self._logged_in = False
            await self.login()
            marked = await self._open_qr(qr_hash)
            if marked is None:
                raise LmsTemporaryError("СДО уводит на вход даже после перелогина")
        return marked

    async def find_active_webinar(self, cmid: int) -> str | None:
        """Ссылка на подключение к идущему сейчас вебинару MTS Link, или None.

        `cmid` — id модуля `mtslinkrnhgs` в курсе (у макро — 1236024). На
        странице модуля раздел «Текущие вебинары»: если вебинар идёт, там есть
        ссылка `my.mts-link.ru/event/…`; если нет — «Нет активных вебинаров».
        Записи прошедших (`…/record-new/…`) сюда не попадают — фильтр по шаблону.

        Оговорка: если СДО начнёт подставлять ссылку скриптом уже в браузере, а
        не в HTML, парсинг вернёт None и живую ссылку придётся брать из Playwright
        или из AJAX модуля — проверим на первом живом вебинаре.
        """
        if not self._logged_in:
            await self.login()
        page = await self._fetch_module(cmid)
        match = _MTS_LIVE_RE.search(page)
        return match.group(0) if match else None

    async def _fetch_module(self, cmid: int) -> str:
        """GET страницы модуля с одним перелогином при истёкшей сессии."""
        path = f"/mod/mtslinkrnhgs/view.php?id={cmid}"
        try:
            resp = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise LmsTemporaryError(f"СДО не отдала модуль вебинаров: {exc}") from exc
        if "/login/index.php" in str(resp.url):
            self._logged_in = False
            await self.login()
            try:
                resp = await self._client.get(path)
            except httpx.HTTPError as exc:
                raise LmsTemporaryError(f"СДО не отдала модуль вебинаров: {exc}") from exc
        return resp.text

    async def _open_qr(self, qr_hash: str) -> Marked | None:
        """Открыть ссылку отметки. None — если СДО потребовала войти заново."""
        try:
            resp = await self._client.get(
                f"/mod/attendancernhgs/qrcode.php?qr={qr_hash}"
            )
        except httpx.HTTPError as exc:
            raise LmsTemporaryError(f"СДО не ответила на отметку: {exc}") from exc

        if "/login/index.php" in str(resp.url):
            return None
        return _classify(resp.text)


def _visible_text(html: str) -> str:
    """Грубо выдрать видимый текст: убрать скрипты и теги, схлопнуть пробелы.

    Достаточно, чтобы поймать короткое сообщение Moodle об исходе отметки —
    полноценный парсер HTML тут был бы избыточен.
    """
    html = re.sub(r"<script[\s\S]*?</script>", " ", html)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _classify(html: str) -> Marked:
    text = _visible_text(html)
    low = text.lower()
    # Успех проверяем первым: на его странице маркеров «протухло» не бывает,
    # но порядок делает намерение явным.
    if any(marker in low for marker in _SUCCESS_MARKERS):
        return Marked(MarkResult.MARKED, _success_summary(text))
    if any(marker in low for marker in _EXPIRED_MARKERS):
        return Marked(MarkResult.EXPIRED, "QR-код уже неактивен — нужен свежий кадр.")
    return Marked(MarkResult.UNKNOWN, _extract_message(text))


def _success_summary(text: str) -> str:
    """Собрать короткое подтверждение из зелёного блока Moodle.

    В блоке идут поля «Дисциплина», «Расписание», «Статус». Текст уже
    схлопнут в одну строку, поэтому каждое поле берём до следующего ярлыка.
    Если разметка изменится и полей не окажется — не падаем, отдаём общее
    «присутствие засчитано».
    """
    discipline = _field(text, "Дисциплина", until=("Расписание", "Статус"))
    schedule = _field(text, "Расписание", until=("Статус",))
    status = _field(text, "Статус", until=())
    lines = []
    if discipline:
        lines.append(discipline)
    if schedule:
        lines.append(f"Пара: {schedule}")
    if status:
        lines.append(f"Статус: {status}")
    return "\n".join(lines) if lines else "Присутствие засчитано."


def _field(text: str, label: str, *, until: tuple[str, ...]) -> str | None:
    """Значение поля `label:` до следующего ярлыка из `until` или до конца."""
    start = text.find(label + ":")
    if start < 0:
        return None
    start += len(label) + 1
    end = len(text)
    for stop in until:
        pos = text.find(stop + ":", start)
        if 0 <= pos < end:
            end = pos
    return text[start:end].strip(" .,-") or None


def _extract_message(text: str) -> str:
    """Вырезать короткий осмысленный кусок из простыни Moodle для показа в чат."""
    for keyword in ("отмеч", "присут", "засчит", "посещаем", "qr"):
        idx = text.lower().find(keyword)
        if idx >= 0:
            return text[max(0, idx - 20) : idx + 90].strip()
    return text[:120].strip()
