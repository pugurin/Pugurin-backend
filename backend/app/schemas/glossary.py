from uuid import UUID

from pydantic import BaseModel

from app.repositories.types import GlossaryCategory, GlossaryTerm
from app.schemas.common import PlainEnvelope


class GlossaryTermOut(BaseModel):
    id: UUID
    term: str
    category: GlossaryCategory
    is_popular: bool
    display_order: int
    short_definition: str
    long_definition: str
    example: str

    @classmethod
    def from_domain(cls, t: GlossaryTerm) -> "GlossaryTermOut":
        return cls(**t.__dict__)


GlossaryTermResponse = PlainEnvelope[GlossaryTermOut]
GlossaryListResponse = PlainEnvelope[list[GlossaryTermOut]]
