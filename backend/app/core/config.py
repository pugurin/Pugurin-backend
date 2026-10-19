from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUGURIN_", env_file=".env", extra="ignore")

    app_env: str = "local"

    # DB·Redis (#38). 비워 두면 지금처럼 파일 캐시·메모리만으로 동작한다
    database_url: str = ""  # 예: postgresql+asyncpg://pugurin:pugurin@localhost:5432/pugurin
    redis_url: str = ""  # 예: redis://localhost:6379/0

    # 부산 대략 범위(min_lng, min_lat, max_lng, max_lat). 1차 판별용, 정밀 판별은 PostGIS로 대체 예정
    busan_bbox: tuple[float, float, float, float] = (128.75, 34.85, 129.35, 35.40)

    # 줌 → 집계 단위 임계치 (표준 웹 메르카토르 줌). 정책 #4 확정 전 기본값
    zoom_dong_min: int = 12
    zoom_complex_min: int = 14
    max_markers: int = 500

    # 공공데이터포털 실거래가 키. 매매 API용과 전월세 API용이 따로 발급될 수 있다. 둘 다 있어야 실제 API를 쓴다
    data_go_kr_trade_key: str = ""
    data_go_kr_rent_key: str = ""
    molit_base_url: str = "https://apis.data.go.kr/1613000"
    molit_daily_call_limit: int | None = None

    # 카카오 로컬(REST 키): 지번 주소 → 좌표·법정동 코드. 수집 명령(refresh)에서만 쓴다
    kakao_rest_key: str = ""

    # sample: 가짜 샘플 / real: `refresh`로 모은 실데이터(.cache) / auto: .cache가 있으면 real, 없으면 sample
    data_mode: Literal["sample", "real", "auto"] = "sample"
    data_dir: Path = Path(".cache")
    real_data_months: int = 12
    worker_run_hour: int = 4  # worker가 매일 수집·적재를 시작하는 시각(KST). API 명세의 data_as_of 기준(04:00)과 맞춘다
    exclude_share_deals: bool = True  # 토지 지분거래는 가격이 왜곡되어 집계에서 뺀다
    exclude_road_land: bool = True  # 지목이 '도로'인 토지 거래도 뺀다

    cache_max_age_seconds: int = 300

    # 분당 요청 한도. 통신사 NAT로 여러 사람이 IP를 같이 쓰므로 IP 한도는 비정상 트래픽 방어용 상한만 둔다
    rate_limit_enabled: bool = True
    rate_limit_device_per_minute: int = 300
    rate_limit_ip_per_minute: int = 3000
    parcel_cache_ttl_days: int = 30
    parcel_negative_cache_ttl_days: int = 1
