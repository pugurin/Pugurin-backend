"""자체 토큰. access token은 JWT(HS256), refresh·가입 토큰은 추측할 수 없는 임의 문자열이고 DB에는 해시만 둔다."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import jwt

ACCESS_TTL = timedelta(minutes=30)
REFRESH_TTL = timedelta(days=14)
SIGNUP_TTL = timedelta(minutes=10)
MIN_SECRET_BYTES = 32
_ALGORITHM = "HS256"


class InvalidToken(Exception):
    """서명·형식이 틀렸거나 만료된 토큰. 이유는 응답에 드러내지 않는다."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: UUID
    role: str


class JwtSigner:
    """access token 발급·검증. 만료는 PyJWT의 시스템 시계가 아니라 주입한 시각으로 판단한다(테스트에서 시계를 고정)."""

    def __init__(self, secret: str):
        if len(secret.encode()) < MIN_SECRET_BYTES:
            raise ValueError(f"JWT 서명 키는 {MIN_SECRET_BYTES}바이트 이상이어야 합니다")
        self._secret = secret

    def issue(self, user_id: UUID, role: str, *, now: datetime) -> str:
        iat = int(now.timestamp())
        payload = {"sub": str(user_id), "role": role, "iat": iat, "exp": iat + int(ACCESS_TTL.total_seconds())}
        return jwt.encode(payload, self._secret, algorithm=_ALGORITHM)

    def verify(self, token: str, *, now: datetime) -> AccessClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                options={"require": ["sub", "role", "exp", "iat"], "verify_exp": False, "verify_iat": False},
            )
            if not isinstance(payload["exp"], int) or now.timestamp() >= payload["exp"]:
                raise InvalidToken
            return AccessClaims(UUID(payload["sub"]), str(payload["role"]))
        except (jwt.PyJWTError, ValueError, TypeError) as e:
            raise InvalidToken from e


def new_opaque_token() -> str:
    """refresh·가입 토큰. 256비트 임의 값(URL에 그대로 써도 되는 문자만)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """DB에 두는 값. 토큰 자체가 256비트 임의 값이라 솔트 없는 SHA-256으로 충분하다."""
    return hashlib.sha256(token.encode()).hexdigest()
