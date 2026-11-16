"""자체 토큰: access token(JWT HS256), refresh·가입 토큰(임의 문자열 + SHA-256 해시)."""

import base64
import json
from datetime import timedelta
from uuid import uuid4

import jwt
import pytest

from app.auth.tokens import (
    ACCESS_TTL,
    AccessClaims,
    InvalidToken,
    JwtSigner,
    hash_token,
    new_opaque_token,
)
from tests.conftest import FIXED_NOW

SECRET = "t" * 32


def test_access_token_carries_subject_role_and_times():
    user_id = uuid4()
    token = JwtSigner(SECRET).issue(user_id, "buyer", now=FIXED_NOW)
    payload = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False, "verify_iat": False})
    assert payload["sub"] == str(user_id) and payload["role"] == "buyer"
    assert payload["iat"] == int(FIXED_NOW.timestamp())
    assert payload["exp"] - payload["iat"] == int(ACCESS_TTL.total_seconds()) == 30 * 60
    header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert header["alg"] == "HS256"


def test_access_token_verifies_with_the_injected_clock_until_it_expires():
    signer = JwtSigner(SECRET)
    user_id = uuid4()
    token = signer.issue(user_id, "admin", now=FIXED_NOW)
    assert signer.verify(token, now=FIXED_NOW + ACCESS_TTL - timedelta(seconds=1)) == AccessClaims(user_id, "admin")
    with pytest.raises(InvalidToken):
        signer.verify(token, now=FIXED_NOW + ACCESS_TTL)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),  # 서명 변조
        lambda t: "",
        lambda t: "abc.def",
        lambda t: jwt.encode({"sub": "x", "exp": 9999999999, "iat": 0, "role": "buyer"}, "o" * 32, algorithm="HS256"),
        lambda t: jwt.encode({"sub": "x", "exp": 9999999999, "iat": 0, "role": "buyer"}, None, algorithm="none"),
        lambda t: jwt.encode({"sub": "not-a-uuid", "exp": 9999999999, "iat": 0, "role": "buyer"}, SECRET),
        lambda t: jwt.encode({"sub": str(uuid4()), "iat": 0, "role": "buyer"}, SECRET),  # exp 없음
    ],
)
def test_tampered_foreign_or_malformed_tokens_are_rejected(mangle):
    signer = JwtSigner(SECRET)
    token = mangle(signer.issue(uuid4(), "buyer", now=FIXED_NOW))
    with pytest.raises(InvalidToken):
        signer.verify(token, now=FIXED_NOW)


def test_short_secrets_are_refused():
    with pytest.raises(ValueError):
        JwtSigner("s" * 31)


def test_opaque_tokens_are_long_random_and_only_their_hash_is_kept():
    a, b = new_opaque_token(), new_opaque_token()
    assert a != b and len(a) >= 43
    assert hash_token(a) == hash_token(a) != hash_token(b)
    assert len(hash_token(a)) == 64 and a not in hash_token(a)
