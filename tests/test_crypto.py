"""Проверки шифрования учётных данных и секретов ICS-ссылок."""

from __future__ import annotations

import base64
import os

import pytest

from app.core.crypto import CredentialsCipher, CredentialsCipherError, new_feed_token


def make_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_roundtrip_preserves_password():
    cipher = CredentialsCipher(make_key())
    secret = "П@роль с пробелом и юникодом"

    assert cipher.decrypt(cipher.encrypt(secret)) == secret


def test_ciphertext_differs_between_calls():
    """Одинаковые пароли не должны давать одинаковый шифротекст.

    Иначе по дампу базы видно, у каких пользователей пароль совпадает.
    """
    cipher = CredentialsCipher(make_key())

    assert cipher.encrypt("одинаковый") != cipher.encrypt("одинаковый")


def test_plaintext_absent_from_ciphertext():
    cipher = CredentialsCipher(make_key())

    assert "hunter2" not in cipher.encrypt("hunter2")


def test_foreign_key_cannot_decrypt():
    stored = CredentialsCipher(make_key()).encrypt("пароль")

    with pytest.raises(CredentialsCipherError):
        CredentialsCipher(make_key()).decrypt(stored)


def test_corrupted_record_reports_clearly():
    cipher = CredentialsCipher(make_key())

    with pytest.raises(CredentialsCipherError):
        cipher.decrypt("не шифротекст")


def test_bad_key_fails_at_construction():
    """Кривой ключ должен ронять приложение на старте, а не при первом входе."""
    with pytest.raises(CredentialsCipherError):
        CredentialsCipher("слишком короткий ключ")


def test_feed_tokens_are_unique_and_long():
    tokens = {new_feed_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(token) >= 32 for token in tokens)
