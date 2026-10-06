# backend/AGENTS.md

부산 부동산 플랫폼의 **백엔드**를 개발하는 코딩 에이전트용 가이드입니다.
전체 개요·단계별 범위·공통 규칙은 루트 [`../AGENTS.md`](../AGENTS.md), API 계약은 [`../docs/API_SPEC.md`](../docs/API_SPEC.md)를 참고하세요.

---

## 1. 역할

- 실거래가를 **배치로 수집·정제·지오코딩·적재(ETL)** 하고, 단지·건물 단위로 묶는다.
- 지도 영역·줌·필터로 **마커/집계를 조회하는 API**를 제공한다.
- 필지(PNU)의 **토지이용계획·토지특성을 온디맨드 조회 후 캐싱**한다.
- **소셜 로그인 → JWT 발급**, role(`buyer`/`agent`/`admin`) 기반 인가를 제공한다.
- **구매자 ↔ 중개사 실시간 채팅**을 중계한다. (사용자 간 채팅은 범위 밖)
- 알림(푸시)·신고·관리자 기능을 제공한다.
- 광고는 앱이 외부 SDK로 처리하므로 백엔드 범위가 아니다.

## 2. 기술 스택

| 항목             | 선택                                                           |
| ---------------- | --------------------------------------------------------------- |
| 프레임워크       | FastAPI (async)                                                 |
| DB               | PostgreSQL 16 + PostGIS                                         |
| ORM/마이그레이션 | SQLAlchemy 2.x (async) + GeoAlchemy2 + Alembic                  |
| 캐시/메시징      | Redis — 캐시, rate limit, 채팅 pub/sub, 조회수 카운터            |
| 인증             | 카카오·네이버 OAuth → 자체 JWT(access 30분 / refresh 14일, rotation) |
| 실시간 통신      | FastAPI WebSocket + Redis pub/sub                               |
| 배치             | 별도 ETL 워커 컨테이너 + 스케줄러(cron 또는 APScheduler)         |
| 푸시             | FCM (APNs는 FCM 경유)                                           |
| 파일             | S3 호환 스토리지, presigned URL, `public/`·`private/` prefix 분리 |
| 테스트           | pytest + testcontainers(PostGIS, Redis)                         |
| 실행/배포        | Docker Compose (api, worker, db, redis)                         |

## 3. 레이어 구조

```
routers/       HTTP/WS 진입점. 요청 검증·직렬화만. 외부 API 직접 호출 금지.
services/      도메인 로직(지도 집계, 통계, 분석 조합, 채팅 라우팅, 알림 생성).
repositories/  DB 접근(PostGIS 공간 쿼리 포함).
adapters/      외부 API 어댑터(실거래가 / VWorld / 국세청 / OAuth / FCM / 스토리지) + mock 구현.
ingestion/     ETL 잡(수집·정제·지오코딩·단지 매칭·적재). API 서버 프로세스에서 실행하지 않는다.
realtime/      WebSocket 연결 관리 + Redis pub/sub 브리지. 도메인 로직 없음.
auth/          OAuth 연동, JWT 발급·검증, role 기반 의존성.
models/        SQLAlchemy 모델.
schemas/       Pydantic 요청/응답 스키마. (models와 섞지 않는다)
core/          설정(config), DB·Redis 세션, 에러 핸들러, 공통 유틸.
sample_data/   mock 모드 전용 가짜 데이터(지역·거래·용어). 운영 경로에서 import 금지.
wiring.py      의존성 조립(composition root). 구현체(memory/mock → DB/실 어댑터)를 services에 주입.
```

호출 방향: `routers → services → repositories / adapters`. 역방향 의존 금지.

## 4. 백엔드 규칙

### 4.1 공통
1. **API 키 없이도 개발·테스트 가능해야 한다.** 외부 접근은 `adapters/` 인터페이스 뒤에 두고 mock/샘플 구현을 기본 제공. 실 키는 환경변수, 하드코딩 금지.
2. **비밀값 커밋 금지.** `.env`는 `.gitignore`, `.env.example`만 커밋.
3. **TDD.** 라우터·서비스·리포지토리는 테스트 우선. 외부 API는 mock, **공간 쿼리는 testcontainers로 띄운 실제 PostGIS에서 검증**(mock DB로는 공간 쿼리 정합성을 확인할 수 없다).
4. 응답·주석·문서는 한국어. 에러는 명세의 공통 JSON 형식으로 반환.
5. 라우트 선언 시 **고정 경로(`/agents/me/...`)를 경로 변수(`/agents/{id}`)보다 먼저** 등록한다.

### 4.2 공간 데이터
1. 좌표는 `geometry(Point, 4326)` + GiST 인덱스로 저장. 영역 조회는 `ST_Intersects`/`ST_MakeEnvelope`.
2. **거리 조회는 반드시 미터 단위로.** 4326 geometry에 `ST_DWithin`을 쓰면 단위가 도(degree)다. `geom::geography`로 캐스팅하거나(`ST_DWithin(geom::geography, :pt::geography, :meters)`), 필요 시 EPSG:5186 컬럼을 별도로 둔다.
3. **좌표는 서버가 만든다.** 매물·중개사무소·실거래가 모두 주소/지번을 받아 VWorld 지오코딩으로 좌표를 산출한다. 클라이언트가 보낸 좌표는 신뢰하지 않는다.
4. 필지 식별은 **PNU(19자리)** 로 통일한다.

### 4.3 실거래가 ETL
1. **부산 16개 구·군 법정동 코드만** 수집한다. 지역·유형은 설정으로 관리.
2. **멱등(idempotent) upsert.** 원천에 거래 고유 ID가 없으므로 유형별 **복합 자연키**를 정의한다.
   - 예: `(property_type, deal_type, 시군구코드, 법정동코드, 지번, 단지/건물명, 전용면적, 층, 계약일, 금액)` → 해시하여 `source_key` unique 인덱스.
   - 동일 자연키 중복(같은 날 같은 층 같은 금액)은 원천 순번으로 구분한다.
3. **최근 3개월은 매 실행마다 재수집한다.** 신고 기한(30일)과 정정·해제 신고로 과거 월 데이터가 계속 바뀐다.
4. **해제(취소) 거래 처리.** `is_cancelled`, `cancelled_at`을 저장하고, 통계·기본 지도 노출에서 제외한다.
5. **거래유형(중개/직거래) 저장.** 직거래는 특수관계 저가거래가 섞일 수 있어 통계 필터 옵션으로 제공한다.
6. **단지·건물 매칭.** 아파트·오피스텔·연립은 거래를 `complexes`(단지/건물)에 연결한다. 지도 마커의 단위는 단지다.
7. **위치 정밀도 표시.** 지번이 마스킹된 거래(토지 등, 확인 필요)는 `location_precision = 'dong'`으로 법정동 중심점에 두고 필지 마커로 표시하지 않는다.
8. **금액 단위 변환.** 원천(만원) → 저장(원, bigint).
9. **적재 완료 후 후처리 훅:** 집계 테이블 갱신 → 관심단지·관심지역 알림 생성 → `data_as_of` 갱신.
10. **트래픽 한도 준수.** 호출 수를 계측하고, 한도 초과 시 다음 실행으로 이월. 백필은 분할 실행.

### 4.4 조회·캐시
1. 공개 조회 API는 `Cache-Control` + `ETag`를 붙인다(실거래가는 일 1회 갱신).
2. 토지이용계획·토지특성 캐시 키는 **PNU**, TTL 30일. 좌표 조회는 `좌표 → PNU` 변환 후 캐시를 조회한다.
   - 캐시에는 필지 **경계 도형**(연속지적도)도 함께 저장한다. 응답 `geometry`는 표시용으로 단순화(`ST_SimplifyPreserveTopology`, 오차 0.5m 이내)해서 내린다.
   - 용도지역 **면적 비율**(`zonings[].area_ratio`)은 필지 도형과 VWorld 용도지역 레이어(`LT_C_UQ111` 등) 도형의 교차 면적(`ST_Area(ST_Intersection(...)::geography)`)으로 계산해 함께 캐시한다.
   - 좌표에 필지가 없거나(도로·바다) 부산 밖이면 예외가 아니라 `unavailable_reason`(`no_parcel`/`out_of_service_area`)으로 반환한다. `no_parcel` 결과도 좌표 격자 단위로 짧게(1일) 캐시해 반복 호출을 막는다.
3. **건폐율·용적률 한도**는 VWorld가 아니라 `zoning_rules`(부산시 도시계획 조례 기준 용도지역별 한도) 테이블에서 산출한다. (VWorld 제공 여부 확인 전까지 이 방식 기준)
4. 통계는 **중위값** 기준, `property_type`은 단일 값 필수, 해제거래 제외.
5. 조회수 등 카운터는 Redis에서 증가시키고 주기적으로 DB에 반영한다(읽기 요청마다 DB 쓰기 금지).

### 4.5 인증·인가
1. 로그인은 **카카오·네이버 OAuth만** 제공한다. 이메일/비밀번호 가입·재설정은 만들지 않는다.
2. refresh token은 **body로 발급·수신**(앱 보안 저장소 보관, 쿠키 미사용). 서버에 해시 저장, **사용 시마다 rotation**, 폐기된 토큰 재사용 감지 시 해당 사용자의 전체 세션 폐기.
3. role 기반 의존성(`require_role("agent", approved=True)`)으로 권한을 분리한다. 중개사는 `buyer` 계정에서 **검증 신청으로 승격**된다.
4. 중개사 검증은 **자동 우선**: 국세청 사업자 상태조회 + VWorld 중개업자 조회로 등록번호·상호·대표자 일치 확인. 실패/불일치 건만 관리자 수동 심사.

### 4.6 채팅
1. **구매자 ↔ 승인된 중개사**만 채팅방을 가진다. 동일 (구매자, 중개사, 매물) 조합은 기존 방을 반환한다.
2. WS는 **상태 비저장 게이트웨이**다. 메시지는 DB에 영속화한 뒤 Redis pub/sub으로 각 인스턴스에 브로드캐스트한다.
3. WS 인증은 **연결 후 최초 프레임의 토큰**으로만 한다(쿼리 파라미터 금지 — 로그 유출). 5초 내 인증 프레임이 없으면 close.
4. **REST 전송 폴백**을 제공하고, 모든 전송에 `client_message_id`를 받아 중복 저장을 막는다(재전송 멱등).
5. 수신자가 WS 미접속이면 푸시 알림을 보낸다.

### 4.7 개인정보·법령
1. **회원 탈퇴 시 개인정보는 즉시 파기(익명화)** 하고, 법정 보관 대상만 별도 테이블에 분리 보관한다. 탈퇴 후 같은 소셜 계정으로 재가입 가능해야 한다.
2. 광고성 푸시는 **수신동의 일시**, **야간(21~08시) 별도 동의**를 기록하고 **2년마다 재동의**를 받는다.
3. 매물(3차)은 **공인중개사법 제18조의2 표시·광고 명시사항**을 필수 필드로 강제한다(명세 §9).
4. 중개사 자격 서류는 `private/` prefix에 저장하고 관리자만 서명 URL로 조회한다.

## 5. 설계 예정 (TODO)

- [ ] DB 스키마 초안 (transactions, complexes, parcels_cache, zoning_rules, users, agents, chat, notifications, reports)
- [ ] 거래 유형별 복합 자연키 확정 (실제 응답 확인 후)
- [ ] 줌 레벨별 집계 단위 임계치 확정 (현재 기본값 12 / 14, `PUGURIN_ZOOM_*` 설정)
- [ ] PostGIS 저장소 구현 시 `repositories/memory/*` 대체 + testcontainers 공간 쿼리 테스트
- [ ] 필지 어댑터를 VWorld 실구현으로 교체(`adapters/mock/parcel_source.py` 대체), 캐시를 Redis로
- [ ] 부산 여부 판별을 bbox 근사 → PostGIS/PNU 기준으로 교체
- [ ] 부산시 도시계획 조례 기준 `zoning_rules` 시드 데이터
- [ ] 신고 신뢰도 점수 모델
- [ ] Alembic 초기 세팅, Docker Compose
- [x] `.env.example`, mock 샘플 데이터(16개 구·군 집계 + 동 37곳·단지 190곳·거래 약 10만 건, 모두 가짜)
- [x] 1차 mock API: `/map/markers`, `/complexes/{id}`(+`/transactions`), `/parcels/lookup`·`/parcels/{pnu}`, `/glossary`

## 6. 실행·테스트

mock 모드는 키·DB 없이 바로 실행된다. 모든 데이터는 가짜이며 `/parcels` 응답의 `ratio_source`에도 샘플임을 표시한다.

```bash
cd backend
uv sync
uv run uvicorn --factory app.main:create_app --reload   # http://localhost:8000/docs
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

- 모든 `/api/v1` 조회 API(헬스 제외)는 `X-Device-Id`(UUID) 헤더가 필수다.
- 서버 시작 시 샘플 데이터를 만드느라 1~2초 걸린다.
