from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUGURIN_", env_file=".env", extra="ignore")

    app_env: str = "local"

    # DB·Redis (#38). 비워 두면 지금처럼 파일 캐시·메모리만으로 동작한다
    database_url: str = ""  # 예: postgresql+asyncpg://pugurin:pugurin@localhost:5432/pugurin
    redis_url: str = ""  # 예: redis://localhost:6379/0

    @field_validator("database_url")
    @classmethod
    def _asyncpg_url(cls, url: str) -> str:
        """호스팅이 주는 postgres(ql):// 주소를 asyncpg 형식으로 맞춘다. sslmode는 asyncpg가 ssl로 받는다."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix) :]
        return url.replace("sslmode=", "ssl=")

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

    # 경매마당(경매·공매 물건). 비우면 worker가 경매 동기화를 건너뛴다
    auction_api_key: str = ""
    auction_base_url: str = "https://api.madangs.com"

    # 학교(나이스 교육정보 개방 포털)·병원(공공데이터포털 건강보험심사평가원) 키. 비우면 worker가 그쪽을 건너뛴다
    neis_api_key: str = ""
    hospital_api_key: str = ""
    # 학원(나이스 학원교습소정보). 비우면 neis_api_key를 쓴다(나이스 키는 서비스 구분 없이 쓸 수 있다)
    academy_api_key: str = ""

    # 건축물대장(공공데이터포털 "국토교통부_건축HUB_건축물대장정보 서비스") — 아파트 공급면적. 비우면 건너뛴다
    building_api_key: str = ""
    building_daily_calls: int = 9_000  # 개발계정 하루 1만 회 안에서 남겨 둔다

    # 공공데이터포털 "부산광역시_정비사업 정보" 키(재개발·재건축 구역). 비우면 worker가 정비사업 동기화를 건너뛴다
    redevelopment_api_key: str = ""

    # 학구도(초등학교 통학구역) 파일을 내려받아 둔 곳(`zones/*.shp`, `links/*.csv`). git에 올리지 않는다
    school_zone_dir: Path = Path("data/schoolzone")

    # 실데이터 위치: PUGURIN_DATABASE_URL이 있으면 DB, 없으면 data_dir(.cache).
    # 둘 다 없으면 가짜로 채우지 않고 "데이터 없음"으로 응답한다
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
