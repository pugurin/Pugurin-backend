from app.repositories.types import ZoningLimits
from app.sample_data.zoning import SAMPLE_ZONING_LIMITS, SOURCE_LABEL


class SampleZoningRules:
    source_label = SOURCE_LABEL

    def limits(self, zone_type: str) -> ZoningLimits | None:
        found = SAMPLE_ZONING_LIMITS.get(zone_type)
        return ZoningLimits(*found) if found else None
