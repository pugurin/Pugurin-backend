# Pugurin Backend

부산 부동산 지도 서비스의 백엔드(FastAPI)입니다. 앱은 이 서버의 REST API(`/api/v1`)만 사용합니다.

- API 명세: [`docs/API_SPEC.md`](./docs/API_SPEC.md)
- 기능명세서: [`docs/FEATURE_SPEC.md`](./docs/FEATURE_SPEC.md)
- 서버를 띄운 뒤 Swagger 문서: http://localhost:8000/docs

---

## 데이터는 실데이터만 씁니다

서버는 **가짜 데이터를 내보내지 않습니다.** 실데이터는 아래 순서로 찾습니다.

| 우선순위 | 실데이터 위치 | 언제 |
|---|---|---|
| 1 | DB (`PUGURIN_DATABASE_URL`) | Docker로 띄우면 기본 |
| 2 | 수집 캐시 폴더 `backend/.cache` | DB 없이 직접 실행할 때 |
| 3 | 둘 다 없음 | 지도·거래·통계·검색·분석이 **`503 DATA_NOT_READY`**("실거래 데이터가 아직 없어요")로 응답 |

`backend/.cache`는 공공데이터포털에서 받은 실거래가와 카카오 지오코딩 결과(약 75MB)라서 **Git에 올리지 않습니다**(`.gitignore`). 레포를 클론해서 바로 실행하면 데이터가 없는 상태로 뜹니다.

### 지금 데이터가 있는지 확인

모든 응답에 붙는 헤더 `X-Data-Mode`를 봅니다.

```bash
curl -sI http://localhost:8000/api/v1/health | grep -i x-data-mode
```

- `x-data-mode: real` → 실데이터로 응답 중
- `x-data-mode: none` → 데이터 없음. 아래 방법으로 채우세요

---

## 실데이터로 띄우는 방법

셋 중 하나를 고르세요. **프론트 개발용이라면 A를 추천합니다.** API 키도, 수집 시간도 필요 없습니다.

### A. 백엔드 담당에게 `.cache` 폴더 받기 (추천)

1. 백엔드 담당에게 `backend/.cache` 폴더를 압축 파일로 받습니다. (Git이 아니라 메신저·드라이브로 전달)
2. 레포의 `backend/` 아래에 풀어서 아래 구조가 되게 합니다.
   ```
   backend/
   └── .cache/
       ├── geocode.json
       └── molit/        ← 폴더 1,344개
   ```
3. 서버를 실행합니다 → [서버 실행](#서버-실행)
4. `X-Data-Mode: real`인지 확인합니다.

> 백엔드 담당: 압축은 레포 루트에서 아래 명령으로 만들 수 있습니다.
> ```bash
> tar -czf pugurin-cache.tar.gz -C backend .cache
> ```
> 받은 쪽은 레포 루트에서 아래 명령으로 풉니다.
> ```bash
> tar -xzf pugurin-cache.tar.gz -C backend
> ```

### B. 직접 수집하기

API 키를 직접 받아서 실데이터를 모읍니다. 처음 한 번은 시간이 걸립니다.

**1) API 키 발급**

| 키 | 발급처 | `.env` 이름 |
|---|---|---|
| 공공데이터포털 매매 실거래가 | [data.go.kr](https://www.data.go.kr) — 아파트·오피스텔·연립다세대·토지 매매 API 활용신청 | `PUGURIN_DATA_GO_KR_TRADE_KEY` |
| 공공데이터포털 전월세 실거래가 | [data.go.kr](https://www.data.go.kr) — 아파트·오피스텔·연립다세대 전월세 API 활용신청 | `PUGURIN_DATA_GO_KR_RENT_KEY` |
| 카카오 로컬 REST 키 | [developers.kakao.com](https://developers.kakao.com) — 앱 생성 → REST API 키 | `PUGURIN_KAKAO_REST_KEY` |

공공데이터포털은 계정당 키가 하나라서 매매·전월세 키가 같으면 두 칸에 같은 값을 넣으면 됩니다.

**2) `.env` 작성**

```bash
cd backend
cp .env.example .env
```

`backend/.env`를 열어 키 3개를 채웁니다.

```
PUGURIN_DATA_GO_KR_TRADE_KEY=발급받은키
PUGURIN_DATA_GO_KR_RENT_KEY=발급받은키
PUGURIN_KAKAO_REST_KEY=발급받은키
```

> `.env`는 Git에 올라가지 않습니다. 키를 채팅방이나 커밋에 올리지 마세요.

**3) 수집 실행** (`backend/` 폴더에서)

```bash
uv sync
uv run python -m app.ingestion refresh
```

- 기본값은 부산 16개 구·군 × 7종 × 최근 12개월이고, 실거래가 API를 **약 1,344회**, 카카오 지오코딩을 **약 1만 2천 회** 호출합니다.
- 중간에 끊기거나 일일 한도에 걸려도 **다시 실행하면 이어서 받습니다.**
- 빨리 확인만 하고 싶으면 범위를 줄이세요.
  ```bash
  # 해운대구(26350)만, 최근 3개월만
  uv run python -m app.ingestion refresh --districts 26350 --months 3
  ```

**4) 수집 결과 확인**

```bash
uv run python -m app.ingestion status
```

```
기준 시각 2026-10-06T16:05:22+09:00
거래 138,746건 (원천 140,650건)
단지 13,851곳 (정확한 위치 없음 10), 지역 240곳
```

거래 건수가 나오면 성공입니다. 이제 [서버 실행](#서버-실행)으로 갑니다.

### C. 백엔드 담당 PC의 서버에 바로 붙기

같은 와이파이에 있다면 실데이터를 가진 백엔드 담당 PC의 서버를 그대로 쓸 수 있습니다.

1. 백엔드 담당이 `--host 0.0.0.0`으로 서버를 띄웁니다(아래 [직접 실행](#방법-2-직접-실행-uv) 참고). Docker로 띄우면 이미 외부 접속이 열려 있습니다.
2. 백엔드 담당 PC의 IP를 확인합니다. (macOS: `ipconfig getifaddr en0`)
3. 앱의 API 주소를 `http://<그 IP>:8000/api/v1`로 설정합니다.

> 학교 와이파이처럼 기기 간 통신을 막는 네트워크에서는 연결되지 않을 수 있습니다. 그때는 A를 쓰세요.

---

## 서버 실행

### 방법 1. Docker (추천)

필요한 것: [Docker Desktop](https://www.docker.com/products/docker-desktop/) 또는 OrbStack

레포 루트에서 실행합니다.

```bash
docker compose up --build
```

- `db`(PostGIS), `redis`, `migrate`, `api` 네 개가 뜹니다. `migrate`가 DB 스키마와 부산 법정동 코드를 먼저 넣고 끝나면 `api`가 시작합니다.
  - 호스트의 5432·6379 포트가 이미 쓰이고 있으면 `PUGURIN_DB_PORT=5433 PUGURIN_REDIS_PORT=6380 docker compose up --build`처럼 바꿔서 띄우세요.
- **`api`는 DB에서 조회합니다.** 처음 띄우면 DB가 비어 있어서 지도·거래 API가 `503 DATA_NOT_READY`로 답합니다. 둘 중 하나로 채우세요.
  - 받아 둔 `backend/.cache`가 있으면 DB에 넣기 (API 호출 없음, 2분쯤):
    ```bash
    docker compose --profile worker run --rm worker python -m app.ingestion load
    ```
  - 키가 있으면 worker로 직접 수집: `docker compose --profile worker up -d worker` (아래 "DB에 거래 넣기")
  - 넣은 뒤 서버를 재시작할 필요는 없습니다. 30초 안에 새 데이터를 읽습니다.
- `backend/.env`가 있으면 읽습니다. 없어도 실행됩니다.
- 서버 상태 확인(readiness)은 DB·Redis 연결, 스키마 버전, 적재된 거래가 있는지까지 봅니다. 내부망 전용이라 **컨테이너 안에서** 부르세요.
  ```bash
  docker compose exec api python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/internal/health/ready').read().decode())"
  ```

### 운영 서버에 올리기

리눅스 서버 1대에 같은 구성으로 올립니다(HTTPS 자동, 매일 수집). [`docs/DEPLOY.md`](./docs/DEPLOY.md)를 보세요.

### 방법 2. 직접 실행 (uv)

필요한 것: [uv](https://docs.astral.sh/uv/) (Python 3.12는 uv가 알아서 준비합니다)

```bash
cd backend
uv sync
uv run uvicorn --factory app.main:create_app --reload --host 0.0.0.0 --port 8000
```

`backend/.cache`가 있으면 그 실데이터로, 없으면 "데이터 없음"으로 뜹니다. 명령은 반드시 `backend/` 폴더 안에서 실행하세요. 다른 폴더에서 실행하면 `.env`와 `.cache`를 찾지 못합니다.

---

## 앱에서 호출할 때 주의할 점

### 1. `X-Device-Id` 헤더가 필수입니다

`/health`를 뺀 **모든 API에 UUID 형식의 `X-Device-Id` 헤더**가 있어야 합니다. 없거나 UUID가 아니면 `400 VALIDATION_ERROR`가 납니다.

```bash
curl -H "X-Device-Id: 3f2b8c1e-0000-4000-8000-000000000001" \
  "http://localhost:8000/api/v1/map/markers?bbox=129.10,35.14,129.20,35.19&zoom=11&property_type=apartment"
```

앱 설치 시 UUID를 한 번 만들어 저장해 두고 계속 같은 값을 보내면 됩니다.

### 2. 기기별 서버 주소

| 앱 실행 환경 | API 주소 |
|---|---|
| iOS 시뮬레이터, 웹(같은 PC) | `http://localhost:8000/api/v1` |
| Android 에뮬레이터 | `http://10.0.2.2:8000/api/v1` |
| 실제 휴대폰 | `http://<서버 PC의 IP>:8000/api/v1` (같은 와이파이 필요, 서버는 `--host 0.0.0.0`) |

> Android는 기본적으로 `http://` 요청을 막습니다. 개발 중에는 `usesCleartextTraffic` 설정이 필요할 수 있습니다.

### 3. 아직 제공하지 않는 것

| 기능 | 상태 | 이유 |
|---|---|---|
| 토지 정보 `/parcels` | 항상 `data: null`, `unavailable_reason: "source_unavailable"` | 토지이용계획·토지특성 공급처가 아직 정해지지 않았습니다 (VWorld 미사용) |
| 주소 검색 (`/search`에 번지가 있는 검색어) | 카카오 REST 키가 있을 때만 결과가 나옵니다 | 키가 없으면 단지·동네 검색만 됩니다 |
| 건폐율·용적률 한도 | 내지 않습니다 | 부산시 도시계획 조례 표를 확인하기 전이라 추정값을 내보내지 않습니다 |
| 토지 실거래가 위치 | 동네(법정동) 단위로만 나옵니다 | 원천 데이터에서 지번이 가려져 있습니다 |

---

## 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| `503 DATA_NOT_READY`, `X-Data-Mode: none` (직접 실행) | `backend/.cache`가 없음 | 위 [A](#a-백엔드-담당에게-cache-폴더-받기-추천) 또는 [B](#b-직접-수집하기) 후 서버 재시작 |
| `503 DATA_NOT_READY` (Docker) | DB가 비어 있음 | 아래 "DB에 거래 넣기"의 `load` 실행. 서버 재시작은 필요 없음 |
| `.cache` 위치가 맞는지 모르겠음 | 폴더가 한 단계 더 들어가 있음 (`backend/.cache/.cache/...`) | `backend/.cache/molit`이 바로 보여야 합니다 |
| 모든 요청이 `400`, `field: X-Device-Id` | 헤더 없음 또는 UUID가 아님 | UUID 형식 `X-Device-Id` 헤더 추가 |
| 갑자기 `429 RATE_LIMITED` | 같은 기기에서 분당 300회 초과 (또는 같은 IP에서 3,000회) | `Retry-After` 초만큼 기다리면 풀립니다. 개발 중이면 `backend/.env`에 `PUGURIN_RATE_LIMIT_ENABLED=false` |
| `refresh`가 키가 필요하다며 종료 | `.env`에 키가 비어 있음 | 매매·전월세 키 둘 다, 카카오 키까지 입력 |
| `refresh` 중 한도 초과로 멈춤 | 공공데이터포털·카카오 일일 한도 | 다음 날 다시 실행하면 이어서 받습니다 |
| 지도 영역은 맞는데 마커가 하나도 없음 | 조회 영역이 부산 밖이거나 필터가 너무 좁음 | `bbox` 순서 확인: `최소경도,최소위도,최대경도,최대위도` |

---

## 개발 (백엔드)

```bash
cd backend
uv sync
uv run pytest          # 테스트
uv run ruff check .    # 린트
```

- `tests/test_db.py`, `tests/test_db_ingest.py`는 testcontainers로 **실제 PostGIS·Redis 컨테이너**를 띄워 마이그레이션·시드·readiness·적재를 검사합니다. Docker가 없으면 자동으로 건너뜁니다.

### DB 마이그레이션

스키마는 [`docs/ERD.md`](./docs/ERD.md)의 DDL이 기준이고 `backend/alembic/versions/`에 있습니다. 로컬 DB에 직접 적용하려면 다음과 같이 합니다.

```bash
cd backend
export PUGURIN_DATABASE_URL=postgresql+asyncpg://pugurin:pugurin@localhost:5432/pugurin
uv run python -m app.db upgrade        # 최신 스키마 + 법정동 시드
uv run python -m app.db status         # 현재 버전 확인
uv run python -m app.db seed-regions   # 법정동 CSV를 갱신한 뒤 다시 반영
```

법정동 코드는 공공데이터포털 「[국토교통부_전국 법정동](https://www.data.go.kr/data/15063424/fileData.do)」에서 부산·현존 코드만 잘라 `backend/app/db/seed/legal_dong_busan.csv`로 둡니다. 연 1회 새 파일로 갱신합니다.

### DB에 거래 넣기

`PUGURIN_DATABASE_URL`이 있으면 수집 명령이 끝날 때 DB에도 넣습니다. 이미 받아 둔 `.cache`만 넣을 때는 `load`를 씁니다(API를 부르지 않음).

```bash
cd backend
export PUGURIN_DATABASE_URL=postgresql+asyncpg://pugurin:pugurin@localhost:5432/pugurin
uv run python -m app.ingestion load      # .cache → DB. 이미 넣은 단위는 건너뜀 (--force면 전부 다시)
uv run python -m app.ingestion refresh   # 수집 → 지오코딩 → DB 적재 (DB를 건너뛰려면 --no-db)
uv run python -m app.db status           # 스키마 버전, 거래 건수, 마지막 적재 결과
```

- 처음 넣을 때는 12개월치(약 14만 건)에 2분쯤 걸립니다. 다시 실행하면 바뀐 단위만 넣습니다.
- 같은 거래를 다시 넣어도 중복되지 않습니다. 다시 받은 달에서 사라진 거래는 지우지 않고 `deleted_at`으로 표시합니다.
- 매일 자동으로 돌리려면 worker를 띄웁니다. `backend/.env`에 공공데이터포털 키 2개와 카카오 키가 있어야 하고, 시작할 때 한 번, 이후 매일 04시(KST)에 수집·적재합니다.
  ```bash
  docker compose --profile worker up -d worker
  docker compose logs -f worker
  ```

개발 규칙은 [`backend/AGENTS.md`](./backend/AGENTS.md)를 따릅니다.
