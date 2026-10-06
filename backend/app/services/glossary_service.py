from uuid import UUID

from app.core.errors import not_found
from app.repositories.ports import GlossaryRepository
from app.repositories.types import GlossaryCategory, GlossaryTerm


class GlossaryService:
    def __init__(self, repo: GlossaryRepository):
        self._repo = repo

    async def list_terms(
        self, *, q: str | None, category: GlossaryCategory | None, popular: bool | None
    ) -> list[GlossaryTerm]:
        return await self._repo.list_terms(q=q, category=category, popular=popular)

    async def get(self, term_id: UUID) -> GlossaryTerm:
        found = await self._repo.get_term(term_id)
        if found is None:
            raise not_found("용어를 찾을 수 없습니다")
        return found
