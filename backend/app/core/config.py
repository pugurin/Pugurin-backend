from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUGURIN_", env_file=".env", extra="ignore")

    app_env: str = "local"

    # 부산 대략 범위(min_lng, min_lat, max_lng, max_lat). 1차 판별용, 정밀 판별은 PostGIS로 대체 예정
    busan_bbox: tuple[float, float, float, float] = (128.75, 34.85, 129.35, 35.40)

    # 줌 → 집계 단위 임계치 (표준 웹 메르카토르 줌). 정책 #4 확정 전 기본값
    zoom_dong_min: int = 12
    zoom_complex_min: int = 14
    max_markers: int = 500

    cache_max_age_seconds: int = 300
    parcel_cache_ttl_days: int = 30
    parcel_negative_cache_ttl_days: int = 1
