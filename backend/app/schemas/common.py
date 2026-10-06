from datetime import datetime

from pydantic import BaseModel, Field


class DataMeta(BaseModel):
    data_as_of: datetime
    reporting_lag_notice: bool = True


class PageMeta(DataMeta):
    page: int
    page_size: int
    total: int
    total_pages: int


class EmptyMeta(BaseModel):
    pass


class PlainEnvelope[T](BaseModel):
    data: T
    meta: EmptyMeta = Field(default_factory=EmptyMeta)
