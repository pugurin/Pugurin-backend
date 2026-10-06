from uuid import UUID

from fastapi import APIRouter

from app.repositories.types import GlossaryCategory
from app.routers.deps import ContainerDep
from app.schemas.glossary import GlossaryListResponse, GlossaryTermOut, GlossaryTermResponse

router = APIRouter(tags=["glossary"])


@router.get("/glossary", response_model=GlossaryListResponse, summary="용어 목록")
async def list_glossary(
    container: ContainerDep,
    q: str | None = None,
    category: GlossaryCategory | None = None,
    popular: bool | None = None,
) -> GlossaryListResponse:
    terms = await container.glossary_service.list_terms(q=q, category=category, popular=popular)
    return GlossaryListResponse(data=[GlossaryTermOut.from_domain(t) for t in terms])


@router.get("/glossary/{term_id}", response_model=GlossaryTermResponse, summary="용어 단건")
async def get_glossary_term(term_id: UUID, container: ContainerDep) -> GlossaryTermResponse:
    return GlossaryTermResponse(data=GlossaryTermOut.from_domain(await container.glossary_service.get(term_id)))
