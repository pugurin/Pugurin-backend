"""관심 등록 서비스의 오류 판정(DB 없이 가짜 저장소로)."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.errors import AppError, ErrorCode
from app.repositories.postgres.interests import AddResult, InterestKind
from app.services.interest_service import InterestService, limit_message


class _Repo:
    def __init__(self, result: AddResult):
        self.result = result

    async def add(self, user_id, kind, target, *, max_items):
        return self.result


class _Complexes:
    async def get(self, complex_id):
        return object()


def _service(result: AddResult) -> InterestService:
    return InterestService(_Repo(result), None, _Complexes(), None, None, lambda: datetime.now(UTC))


def _add(result: AddResult):
    return asyncio.run(_service(result).add_complex(uuid4(), uuid4()))


def test_adding_returns_the_time_it_was_added():
    at = datetime(2026, 11, 24, tzinfo=UTC)
    assert _add(AddResult(added_at=at, member_alive=True)) == at


def test_a_withdrawn_member_gets_401_not_the_limit_message():
    # 인증을 통과한 직후 탈퇴가 커밋되면 넣지 못한다. 한도 초과(409)가 아니라 인증 실패와 같은 401로 답한다
    with pytest.raises(AppError) as e:
        _add(AddResult(added_at=None, member_alive=False))
    assert (e.value.code, e.value.status_code) == (ErrorCode.UNAUTHORIZED, 401)


def test_a_full_list_is_a_conflict():
    with pytest.raises(AppError) as e:
        _add(AddResult(added_at=None, member_alive=True))
    assert (e.value.code, e.value.message) == (ErrorCode.CONFLICT, limit_message(InterestKind.COMPLEX))
