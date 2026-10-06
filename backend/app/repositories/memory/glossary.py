from uuid import UUID

from app.repositories.types import GlossaryCategory, GlossaryTerm
from app.sample_data.glossary import GLOSSARY_TERMS


class InMemoryGlossaryRepository:
    def __init__(self, terms: tuple[GlossaryTerm, ...] = GLOSSARY_TERMS):
        self._terms = tuple(sorted(terms, key=lambda t: (t.display_order, t.term)))
        self._by_id = {t.id: t for t in self._terms}
        self._by_name = {t.term: t for t in self._terms}

    async def list_terms(
        self, *, q: str | None, category: GlossaryCategory | None, popular: bool | None
    ) -> list[GlossaryTerm]:
        needle = q.strip() if q else ""
        return [
            t
            for t in self._terms
            if (category is None or t.category == category)
            and (popular is None or t.is_popular == popular)
            and (not needle or needle in t.term or needle in t.short_definition)
        ]

    async def get_term(self, term_id: UUID) -> GlossaryTerm | None:
        return self._by_id.get(term_id)

    async def get_term_by_name(self, term: str) -> GlossaryTerm | None:
        return self._by_name.get(term)
