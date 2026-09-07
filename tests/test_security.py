"""Password, token and header hardening."""

from __future__ import annotations

import datetime as dt

import jwt
import pytest

from app.config import get_settings
from app.security import hash_password, verify_password
from app.security.passwords import validate_password_strength
from app.security.tokens import (
    ACCESS,
    REFRESH,
    TokenError,
    decode_token,
    make_access_token,
    make_refresh_token,
)


def test_hash_is_argon2id_and_salted():
    first = hash_password("Str0ngPassword!")
    second = hash_password("Str0ngPassword!")
    assert first.startswith("$argon2id$")
    assert first != second  # unique salt per hash
    assert verify_password(first, "Str0ngPassword!")
    assert not verify_password(first, "str0ngpassword!")


def test_verify_rejects_garbage_hash():
    assert verify_password("not-a-hash", "whatever") is False


@pytest.mark.parametrize("weak", ["short1A", "alllowercase123", "NOLOWERCASE123", "NoDigitsHere!!"])
def test_weak_passwords_rejected(weak):
    with pytest.raises(ValueError):
        validate_password_strength(weak)


def test_access_token_roundtrip():
    token, ttl = make_access_token(subject="42", role="manager")
    claims = decode_token(token, expected_kind=ACCESS)
    assert claims["sub"] == "42" and claims["role"] == "manager"
    assert 0 < ttl <= get_settings().access_ttl_minutes * 60


def test_kind_confusion_rejected():
    refresh, _jti, _exp = make_refresh_token(subject="1", family="fam")
    with pytest.raises(TokenError):
        decode_token(refresh, expected_kind=ACCESS)


def test_foreign_signature_rejected():
    forged = jwt.encode(
        {
            "sub": "1",
            "kind": ACCESS,
            "jti": "x",
            "iss": "rwo",
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        },
        "attacker-key",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_token(forged, expected_kind=ACCESS)


def test_expired_token_rejected():
    settings = get_settings()
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    expired = jwt.encode(
        {
            "sub": "1",
            "kind": REFRESH,
            "jti": "y",
            "iss": "rwo",
            "iat": int(past.timestamp()),
            "exp": int((past + dt.timedelta(minutes=1)).timestamp()),
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(expired, expected_kind=REFRESH)
