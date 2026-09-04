"""Шифрование токенов доступа и выдача секретов для ICS-ссылок.

Хранится не пароль, а refresh-токен личного кабинета: пароль нужен один раз при
подключении и никуда не записывается. Это меняет цену утечки — украденный токен
открывает один кабинет и отзывается, украденный пароль обычно подходит ещё
к нескольким сервисам того же человека.

Честно о границах защиты. Токены шифруются симметрично, ключ лежит в окружении
того же сервера, что и база. Это защищает от одного конкретного сценария —
утечки дампа базы (бэкап, украденный снапшот, ошибка в правах доступа). От
того, кто получил доступ к самому серверу, это не защищает: ключ будет рядом.

Отсюда два следствия, которые стоит держать в голове:

* пользователей нужно предупреждать в открытую — этим занимается текст бота
  при подключении аккаунта и README;
* ключ `CREDENTIALS_KEY` не должен попадать в репозиторий, логи и бэкапы базы.
"""

from __future__ import annotations

import secrets

from cryptography.fernet import Fernet, InvalidToken


class CredentialsCipherError(RuntimeError):
    """Расшифровать сохранённый токен не удалось."""


class CredentialsCipher:
    """Обёртка над Fernet для токенов доступа к личному кабинету."""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise CredentialsCipherError(
                "CREDENTIALS_KEY должен быть 32 байтами в base64url. "
                "Сгенерировать: python -c \"import base64,os; "
                'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Расшифровать сохранённый токен.

        Ошибка здесь чаще всего означает не взлом, а смену `CREDENTIALS_KEY`:
        старые записи становятся нечитаемыми, и таких пользователей нужно
        попросить подключить аккаунт заново, а не молча оставить без синхронизации.
        """
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise CredentialsCipherError(
                "Не удалось расшифровать учётные данные: ключ изменился или запись повреждена"
            ) from exc


def new_feed_token() -> str:
    """Секрет для персональной ссылки-подписки.

    Ссылка на фид — это и есть аутентификация: календарь пользователя ходит по
    ней без входа, а значит любой, кто её получил, видит расписание. Поэтому
    токен длинный и случайный, а не производный от идентификатора в Telegram —
    иначе чужие ссылки можно было бы перебрать.
    """
    return secrets.token_urlsafe(32)
