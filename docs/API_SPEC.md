# API 명세서 (Pugurin Backend) — v0.3

부산 부동산 지도 서비스 백엔드 API 명세입니다.
전체 개요·단계별 범위는 [`../AGENTS.md`](../AGENTS.md), 백엔드 구현 규칙은 [`../backend/AGENTS.md`](../backend/AGENTS.md) 참고.

각 섹션 제목의 **[1차]/[2차]/[3차]** 는 구현 단계입니다.

### v0.2 → v0.3 주요 변경
- 지도 조회를 **단지·건물 단위 마커 + 줌별 집계** 단일 엔드포인트(`/map/markers`)로 통합
- **단지(Complexes)**, **검색**, **분석** API 추가
- 토지이용 캐시 키를 좌표 → **PNU**로 변경, **토지특성(공시지가 등)** 추가, 건폐율·용적률은 조례 기준 산출
- 실거래가 **해제거래·거래유형·위치정밀도·데이터 기준일** 반영, 통계는 **중위값**
- 모든 면적 응답에 **평 환산값** 추가
- 인증을 **카카오·네이버 소셜 로그인**으로 전환 (이메일/비밀번호 제거)
- 중개사 가입 순환 의존 해소: **buyer 가입 → 로그인 → 서류 업로드 → 중개사 승격 신청**, 자동 검증 우선
- 채팅: 구매자↔중개사만, WS **최초 프레임 인증**, **REST 전송 폴백 + client_message_id 멱등**, 방 중복 생성 방지
- 매물 등록에 **공인중개사법 표시·광고 명시사항** 필수화 (3차로 이동)
- 삭제: 자체 광고 API, `/glossary/related`, `/chat/rooms/{id}/report`(→ `/reports`로 통합), 서버측 최근 본 매물, 공개 헬스체크의 상세 상태
- Rate limit을 IP 기준 → **디바이스/사용자 기준**으로 변경, 한도 상향
- 관심지역 코드를 **법정동**으로 통일, 관심**단지** 추가
- 탈퇴·마케팅 수신동의를 개인정보보호법·정보통신망법 기준으로 재정의

---

## 0. 공통 규약

**Base URL**: `/api/v1`

**인증**: `Authorization: Bearer <access_token>` (JWT, exp 30분). refresh token(exp 14일)은 **응답 body로 발급**, 앱 보안 저장소에 보관. 쿠키 미사용.

**공통 헤더**
- `X-Device-Id`: 앱 설치 단위 UUID (필수). 비로그인 rate limit·푸시 토큰 매핑에 사용.
- `If-None-Match`: 공개 조회 API는 `ETag` 지원 → 변경 없으면 `304`.

**공통 응답 포맷 (성공)**
```json
{ "data": { }, "meta": { } }
```

**공통 에러 포맷**
```json
{ "error": { "code": "RESOURCE_NOT_FOUND", "message": "단지를 찾을 수 없습니다", "field": null } }
```

**표준 에러 코드**

| code | HTTP | 설명 |
|---|---|---|
| `VALIDATION_ERROR` | 400 | 요청 필드 검증 실패 (`field`에 실패한 필드명) |
| `UNAUTHORIZED` | 401 | 토큰 없음/만료 |
| `FORBIDDEN` | 403 | 권한 없음 (예: 미승인 중개사가 중개사 전용 API 호출) |
| `RESOURCE_NOT_FOUND` | 404 | 대상 없음 |
| `CONFLICT` | 409 | 중복 (예: 이미 진행 중인 중개사 신청) |
| `RATE_LIMITED` | 429 | 요청 과다 (`Retry-After` 헤더 포함) |
| `SOURCE_UNAVAILABLE` | 503 | 외부 데이터 소스 장애 (토지 API는 graceful degrade, §5 참고) |
| `INTERNAL_ERROR` | 500 | 서버 오류 |

**페이지네이션**
- 목록형: offset 방식. 요청 `page`(기본 1), `page_size`(기본 20, 최대 100) / 응답 `meta`: `{ "page", "page_size", "total", "total_pages" }`
- 채팅 메시지: `before_id` 커서 방식(§11)
- 지도 마커(`/map/markers`): 페이지네이션 없음. 서버가 줌에 맞춰 집계 단위를 골라 개수를 제한한다.

**단위·형식 규약**
- 금액: **원 단위 정수**(int64)
- 면적: `*_m2`(㎡, 소수 2자리) + `*_pyeong`(평, 소수 1자리)을 **항상 함께** 반환
- 좌표: WGS84(EPSG:4326). `bbox`는 `min_lng,min_lat,max_lng,max_lat`
- 지역 코드: **법정동 코드**(시군구 5자리 / 법정동 10자리)
- 필지: **PNU** 19자리
- 시각: ISO8601 + 오프셋(`2026-09-29T10:00:00+09:00`), 계약일은 `YYYY-MM-DD`

**거래유형별 가격 필드 의미**

| `deal_type` | `price` | `deposit` | `monthly_rent` |
|---|---|---|---|
| `sale` (매매) | 매매가 | null | null |
| `jeonse` (전세) | null | 보증금 | null |
| `monthly` (월세) | null | 보증금 | 월세 |

- 가격 필터는 `price_*`(매매), `deposit_*`(전·월세), `rent_*`(월세)로 분리한다.
- `property_type=land`는 `deal_type=sale`만 허용 → 그 외 조합은 `VALIDATION_ERROR`.

**데이터 기준일**: 실거래가 관련 응답 `meta`에는 항상 `data_as_of`(마지막 ETL 완료 시각)와 `reporting_lag_notice: true`(최근 30일 거래는 신고 기한으로 누락 가능)를 포함한다.

**Rate limit**
- 비로그인: `X-Device-Id` 기준 분당 300회 / 로그인: 사용자 기준 분당 600회
- IP 기준 제한은 비정상 트래픽 방어용 상한(분당 3,000회)만 둔다 (모바일 통신사 NAT로 다수 사용자가 IP 공유)
- 초과 시 `429 RATE_LIMITED` + `Retry-After`

**버전 관리**: URL 프리픽스(`/api/v1`), breaking change는 `/api/v2` 신설.

---

## 1. 헬스체크 [1차]

| Method | Path | 공개 | 설명 |
|---|---|---|---|
| GET | `/health` | 공개 | liveness. `{ "status": "ok" }`만 반환 |
| GET | `/internal/health/ready` | 내부망 전용 | readiness. DB/Redis/어댑터 상태 상세 (외부 노출 금지) |

---

## 2. 지도 (Map) [1차]

지도 화면의 단일 진입점. 줌 레벨에 따라 서버가 집계 단위를 결정한다.

| Method | Path | 설명 |
|---|---|---|
| GET | `/map/markers` | bbox + zoom + 필터 → 집계 또는 단지/필지 마커 |

**쿼리**

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `bbox` | `min_lng,min_lat,max_lng,max_lat` | ✅ | 지도 뷰포트 |
| `zoom` | int | ✅ | 지도 줌 레벨 |
| `property_type` | `apartment\|officetel\|villa\|land` | ✅ | **단일 선택** (유형 혼합 집계 방지) |
| `deal_type` | `sale\|jeonse\|monthly` | - | 기본 `sale` |
| `period_months` | int (1~60) | - | 기본 12. 최근 N개월 |
| `price_min`, `price_max` / `deposit_min`, `deposit_max` / `rent_min`, `rent_max` | 원 | - | 거래유형별 가격 필터 |
| `area_pyeong_min`, `area_pyeong_max` | 평 | - | 전용면적 기준 |
| `exclude_direct` | bool | - | 직거래 제외 (기본 false) |

**응답 — 줌에 따라 `level`이 달라진다**
```json
{
  "data": {
    "level": "sigungu | dong | complex",
    "markers": [
      {
        "kind": "region",
        "region_code": "2635010500", "name": "우동",
        "lat": 35.16, "lng": 129.16,
        "transaction_count": 128,
        "median_price_per_pyeong": 32000000
      },
      {
        "kind": "complex",
        "complex_id": "uuid", "name": "해운대아이파크",
        "lat": 35.15, "lng": 129.14,
        "latest": { "price": 1450000000, "area_pyeong": 34.2, "contract_date": "2026-08-14" },
        "transaction_count": 21
      },
      {
        "kind": "parcel",
        "transaction_id": "uuid", "pnu": "2635010500100010000",
        "lat": 35.17, "lng": 129.17,
        "latest": { "price": 380000000, "area_pyeong": 120.5, "contract_date": "2026-07-02" }
      }
    ]
  },
  "meta": { "data_as_of": "2026-09-29T04:00:00+09:00", "reporting_lag_notice": true }
}
```
- 해제거래는 항상 제외
- 토지 중 지번 비공개 거래(`location_precision=dong`)는 `parcel` 마커로 내리지 않고 `region` 집계에만 포함
- 서버는 한 응답의 마커 수를 최대 500개로 제한하며, 초과 시 한 단계 상위 레벨로 집계

---

## 3. 검색 (Search) [1차]

| Method | Path | 설명 |
|---|---|---|
| GET | `/search?q=&limit=` | 단지명·주소(도로명/지번)·법정동 통합 검색 |

- `q`는 2자 이상. 결과 최대 `limit`(기본 10, 최대 30)
- 단지·법정동은 자체 DB, 주소는 VWorld 검색/지오코딩 어댑터
- 부산 밖 결과는 제외

**응답**
```json
{
  "data": [
    { "type": "complex", "complex_id": "uuid", "name": "해운대아이파크", "address": "부산 해운대구 우동 1407", "lat": 35.15, "lng": 129.14 },
    { "type": "region", "region_code": "2635010500", "name": "해운대구 우동", "lat": 35.16, "lng": 129.16 },
    { "type": "address", "pnu": "2635010500114070000", "address": "부산 해운대구 우동 1407", "lat": 35.15, "lng": 129.14 }
  ]
}
```

---

## 4. 단지·실거래가 (Complexes & Transactions) [1차]

공공데이터 기반, 읽기 전용, 로그인 불필요.

| Method | Path | 설명 |
|---|---|---|
| GET | `/complexes/{id}` | 단지/건물 정보 |
| GET | `/complexes/{id}/transactions` | 단지 거래 이력 (offset 페이지네이션) |
| GET | `/complexes/{id}/stats` | 단지 평형별 시세 추이 |
| GET | `/transactions` | 목록 화면용 거래 리스트 (bbox 또는 region_code, 페이지네이션) |
| GET | `/transactions/{id}` | 거래 단건 상세 |
| GET | `/stats/regions/{region_code}` | 구·동 단위 통계 |

**`GET /complexes/{id}` 응답**
```json
{
  "id": "uuid", "property_type": "apartment", "name": "해운대아이파크",
  "address": "부산 해운대구 우동 1407", "region_code": "2635010500", "pnu": "2635010500114070000",
  "lat": 35.15, "lng": 129.14, "build_year": 2011, "household_count": 1631,
  "area_types": [
    { "exclusive_area_m2": 84.97, "exclusive_area_pyeong": 25.7, "supply_area_pyeong": 34.0 }
  ]
}
```
- `supply_area_pyeong`(공급 평형)은 건축물대장 기반, 산출 불가 시 `null` (사용자가 흔히 말하는 "34평"은 공급면적 기준)

**거래 1건 필드** (`/transactions`, `/complexes/{id}/transactions` 공통)

`id, property_type, deal_type, complex_id(nullable), address, region_code, jibun(nullable), pnu(nullable), location_precision(parcel|dong), lat, lng, price, deposit, monthly_rent, exclusive_area_m2, exclusive_area_pyeong, price_per_pyeong, floor, contract_date, build_year, trade_method(broker|direct), is_cancelled, cancelled_at`

- `/transactions`는 기본 해제거래 제외, `include_cancelled=true`일 때만 포함(해제 표시 필수)
- 정렬 `sort`: `contract_date_desc`(기본) `|price_asc|price_desc`
- `bbox`와 `region_code` 둘 다 없으면 `400 VALIDATION_ERROR`

**`GET /complexes/{id}/stats`, `GET /stats/regions/{region_code}` 쿼리**: `property_type`(단일, 필수), `deal_type`, `period_months`(기본 36), `exclude_direct`
```json
{
  "data": {
    "median_price_per_pyeong": 38500000,
    "transaction_count": 142,
    "trend": [ { "month": "2026-01", "median_price_per_pyeong": 37800000, "count": 11 } ]
  },
  "meta": { "data_as_of": "...", "reporting_lag_notice": true, "method": "median, 해제거래 제외" }
}
```
- 통계는 **중위값**. 월별 표본 3건 미만인 달은 `median_price_per_pyeong: null`

---

## 5. 토지 (Parcels: 토지이용계획·토지특성) [1차]

| Method | Path | 설명 |
|---|---|---|
| GET | `/parcels/lookup?lat=&lng=` | 좌표 → PNU 변환 후 §5 상세와 동일 응답 |
| GET | `/parcels/{pnu}` | 필지 상세 (토지이용계획 + 토지특성) |

**응답**
```json
{
  "data": {
    "pnu": "2635010500114070000",
    "jibun_address": "부산 해운대구 우동 1407",
    "land_category": "대",
    "area_m2": 1520.3, "area_pyeong": 459.9,
    "official_land_price_per_m2": 5120000,
    "official_land_price_year": 2026,
    "road_side": "중로한면",
    "shape": "가로장방형",
    "zoning": {
      "zone_type": "제2종일반주거지역",
      "max_building_coverage_ratio": 60,
      "max_floor_area_ratio": 250,
      "ratio_source": "부산광역시 도시계획 조례"
    },
    "restrictions": [
      {
        "name": "가축사육제한구역",
        "plain_explanation": "소·돼지 등 가축을 키우는 시설을 지을 수 없어요.",
        "glossary_term_ids": ["uuid"]
      }
    ],
    "glossary_term_ids": ["uuid(건폐율)", "uuid(용적률)", "uuid(공시지가)"]
  },
  "meta": { "cached_at": "2026-09-20T10:00:00+09:00", "source_unavailable": false }
}
```
- 캐시 키 **PNU**, TTL 30일
- 건폐율·용적률 한도는 VWorld가 아닌 **조례 매핑 테이블**에서 산출 (VWorld 제공 여부 확인 전 기준)
- `plain_explanation`은 40~50대가 이해할 수 있는 한 문장. 규제 항목별 해설 사전은 서버가 관리
- 캐시 미스 + VWorld 장애 시: `{ "data": null, "meta": { "source_unavailable": true } }` (지도는 계속 동작하도록 graceful degrade, HTTP 200)
- 좌표가 부산 밖이면 `400 VALIDATION_ERROR (field: lat)`

---

## 6. 분석 (Analysis) [1차]

실거래 추이와 토지 규제를 한 화면에서 보는 요약. **투자 권유·예측 문구는 포함하지 않는다.**

| Method | Path | 설명 |
|---|---|---|
| GET | `/analysis/complexes/{id}` | 단지 시세 추이 + 소속 법정동 추이 비교 + 필지 용도지역 요약 |
| GET | `/analysis/parcels/{pnu}` | 필지 토지특성 + 주변(반경 500m) 동일 지목 토지 거래 중위 평단가 추이 + 규제 요약 |

**`GET /analysis/parcels/{pnu}` 응답 (요약)**
```json
{
  "data": {
    "parcel": { "pnu": "...", "land_category": "대", "zone_type": "제2종일반주거지역", "official_land_price_per_m2": 5120000 },
    "nearby_land_trend": {
      "radius_m": 500, "transaction_count": 18,
      "trend": [ { "year": 2025, "median_price_per_pyeong": 9800000 } ]
    },
    "buildable_summary": "대지 460평 기준 건축면적 최대 약 276평, 연면적 최대 약 1,150평 (조례 한도 기준 단순 계산)",
    "disclaimer": "참고용 정보이며 실제 건축 가능 여부는 관할 구청 확인이 필요합니다."
  },
  "meta": { "data_as_of": "..." }
}
```

---

## 7. 용어 해설 (Glossary) [1차]

| Method | Path | 설명 |
|---|---|---|
| GET | `/glossary` | 전체 목록 (페이지네이션 없음, `ETag`로 캐시). `q` 검색 선택 |
| GET | `/glossary/{term_id}` | 단일 용어 |

응답: `{ "id", "term": "용적률", "short_definition": "툴팁용 쉬운 1줄 설명", "long_definition": "상세 화면용", "example": "string" }`
- 관련 용어 추천 API는 두지 않는다. 필지·단지 응답의 `glossary_term_ids`로 연결한다.

---

## 8. 인증 / 계정 (Auth & Users) [2차]

모든 사용자는 소셜 계정으로 가입한다. role: `buyer`(기본), `agent`(승격), `admin`(내부 지정).

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/auth/oauth/{provider}` | - | `provider: kakao\|naver`. 앱이 받은 OAuth 토큰/인가코드로 로그인·가입 |
| POST | `/auth/refresh` | - (refresh token) | access 재발급 + refresh **rotation** |
| POST | `/auth/logout` | 필요 | 현재 refresh token 폐기 + 해당 디바이스 푸시 토큰 해제 |
| GET | `/users/me` | 필요 | 내 정보 |
| PATCH | `/users/me` | 필요 | 닉네임 수정 |
| PUT | `/users/me/consents` | 필요 | 수신동의 변경 (§12) |
| DELETE | `/users/me` | 필요 | 회원 탈퇴 |

**`POST /auth/oauth/{provider}` 요청 / 응답**
```json
// 요청
{ "access_token": "소셜 SDK가 준 토큰" }
// 응답
{ "data": { "access_token": "jwt", "refresh_token": "opaque", "is_new_user": true, "user": { } } }
```
- 신규 가입 시 필수 약관·개인정보 처리 동의 여부를 `agreements` 필드로 함께 받는다 (`{ "terms": true, "privacy": true }`) — 미동의 시 `VALIDATION_ERROR`
- 폐기된 refresh token 재사용이 감지되면 해당 사용자의 모든 refresh token을 폐기하고 `401`

**`GET /users/me` 응답**
```json
{
  "id": "uuid", "nickname": "string", "role": "buyer|agent|admin",
  "agent_application_status": "none|pending|auto_verified|approved|rejected",
  "consents": {
    "chat_push": true, "interest_alert_push": true,
    "marketing_push": false, "marketing_night_push": false,
    "marketing_consented_at": null, "marketing_reconfirm_due": null
  },
  "created_at": "iso8601"
}
```

**회원 탈퇴 (`DELETE /users/me`)**
- 개인정보(닉네임, 소셜 식별자, 연락처 등)는 **즉시 파기(익명화)**. 법정 보관 대상만 분리 보관 테이블로 이동
- 채팅 메시지는 상대방 화면에 "탈퇴한 사용자"로 표시, 보관 기간 정책(미정) 경과 후 삭제
- 같은 소셜 계정으로 **재가입 가능** (기존 계정과 연결하지 않음)
- 중개사가 탈퇴하면 등록 매물은 즉시 비노출

---

## 9. 중개사 (Agents) [2차]

### 9.1 중개사 승격 신청
`buyer`로 가입·로그인한 뒤 신청한다 (업로드 인증 문제 해소).

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/agents/me/application` | 필요(buyer) | 중개사 승격 신청 |
| GET | `/agents/me/application` | 필요 | 신청 상태·반려 사유 조회 |
| PATCH | `/agents/me/profile` | 중개사(승인) | 사무소 소개, 사진, 취급 지역 수정 |

**`POST /agents/me/application` 요청**
```json
{
  "office_name": "string",
  "representative_name": "string",
  "business_registration_no": "string(10자리)",
  "brokerage_registration_no": "string(중개사무소 등록번호)",
  "office_address": "string",
  "office_phone": "string",
  "license_doc_file_id": "uuid(선택, 자동 검증 실패 시 필수)"
}
```
- 사무소 좌표는 서버가 `office_address`를 지오코딩해 산출 (클라이언트 좌표 미수신)
- **자동 검증:** 국세청 사업자 상태조회(계속사업자 여부) + VWorld 중개업자 조회(등록번호·상호·대표자 일치) → 모두 통과 시 `auto_verified` → 즉시 `approved`
- 불일치/조회 실패 시 `pending` → 관리자 수동 심사(§14). 이때 `license_doc_file_id` 필수
- 이미 `pending`인 신청이 있으면 `409 CONFLICT`
- 승인/반려 시 푸시 알림(`agent_application_result`)

### 9.2 중개사 조회 (공개)

| Method | Path | 설명 |
|---|---|---|
| GET | `/agents?lat=&lng=&radius_m=` | 주변 승인 중개사 목록 (`radius_m` 기본 1000, 최대 5000) |
| GET | `/agents/{id}` | 프로필 |
| GET | `/agents/{id}/listings` | 해당 중개사 매물 [3차] |

**`GET /agents/{id}` 응답**
```json
{
  "id": "uuid", "office_name": "string", "representative_name": "string",
  "brokerage_registration_no": "string", "office_address": "string",
  "office_phone": "string", "lat": 0.0, "lng": 0.0,
  "intro": "string", "photo_url": "string",
  "active_listing_count": 0,
  "chat_response_rate": 0.92, "median_response_minutes": 14
}
```
- `office_phone`은 앱에서 **바로 전화 걸기** 버튼으로 사용 (40~50대 주 연결 수단). 공인중개사법상 공개 대상 정보
- 승인되지 않은 중개사는 목록·상세 모두 `404`
- 라우트는 `/agents/me/...`를 `/agents/{id}`보다 먼저 등록

---

## 10. 관심 단지·지역 (Interests) [2차] / 매물 찜 [3차]

| Method | Path | Auth | 설명 |
|---|---|---|---|
| PUT | `/interests/complexes/{complex_id}` | 필요 | 관심 단지 등록 (멱등) |
| DELETE | `/interests/complexes/{complex_id}` | 필요 | 해제 |
| GET | `/interests/complexes` | 필요 | 목록 (단지별 최신 거래 포함) |
| PUT | `/interests/regions/{region_code}` | 필요 | 관심 법정동 등록 (멱등, 최대 10개) |
| DELETE | `/interests/regions/{region_code}` | 필요 | 해제 |
| GET | `/interests/regions` | 필요 | 목록 |
| PUT | `/interests/listings/{listing_id}` | 필요 | 매물 찜 [3차] |
| DELETE | `/interests/listings/{listing_id}` | 필요 | 찜 해제 [3차] |
| GET | `/interests/listings` | 필요 | 찜 목록 [3차] |

- 등록은 `PUT`으로 멱등 처리 (중복 요청해도 결과 동일)
- 최근 본 매물·단지는 **앱 로컬 저장** (서버 API 없음)

---

## 11. 채팅 (Chat) [2차]

**구매자 ↔ 승인된 중개사** 전용. 사용자 간 채팅은 제공하지 않는다.

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/chat/rooms` | 필요(buyer) | 채팅방 생성 또는 기존 방 반환 |
| GET | `/chat/rooms` | 필요 | 내 채팅방 목록 (마지막 메시지, 안읽음 수) |
| GET | `/chat/rooms/{room_id}` | 필요(참여자) | 방 정보 (상대 프로필, 연결된 단지/매물 요약) |
| GET | `/chat/rooms/{room_id}/messages` | 필요(참여자) | 히스토리 (`before_id` 커서, 기본 30개) |
| POST | `/chat/rooms/{room_id}/messages` | 필요(참여자) | **REST 전송 폴백** (WS 불가 시) |
| POST | `/chat/rooms/{room_id}/read` | 필요(참여자) | 읽음 처리 `{ "last_read_message_id": "uuid" }` |
| POST | `/chat/rooms/{room_id}/leave` | 필요(참여자) | 방 나가기 (내 목록에서 숨김) |
| WS | `/ws/chat` | 최초 프레임 | 실시간 송수신 (한 연결로 내 모든 방 수신) |

**`POST /chat/rooms` 요청**
```json
{ "agent_id": "uuid", "context": { "type": "complex|listing|parcel", "id": "string" } }
```
- 같은 (구매자, 중개사, context) 조합이 있으면 **새로 만들지 않고 기존 방을 `200`으로 반환** (신규는 `201`)
- 중개사가 미승인/탈퇴 상태면 `404`
- 중개사는 방을 먼저 만들 수 없다 (구매자 문의로만 시작 — 스팸 방지)

**`POST /chat/rooms/{room_id}/messages` 요청 (REST·WS 공통 필드)**
```json
{ "client_message_id": "uuid(앱 생성)", "content": "string(1~1000자)" }
```
- 같은 `client_message_id` 재전송 시 **기존 메시지를 그대로 반환** (중복 저장 없음)

**WS 프로토콜**
```json
// 1) 연결 직후 클라→서버 (5초 내 미전송 시 4401 close)
{ "type": "auth", "token": "access_token" }
// 서버→클라
{ "type": "auth_ok" }

// 전송 클라→서버
{ "type": "message", "room_id": "uuid", "client_message_id": "uuid", "content": "string" }
// 서버→클라 (본인에게는 ack 겸용)
{ "type": "message", "id": "uuid", "room_id": "uuid", "client_message_id": "uuid", "sender_id": "uuid", "content": "string", "sent_at": "iso8601" }
{ "type": "read_receipt", "room_id": "uuid", "reader_id": "uuid", "last_read_message_id": "uuid" }
{ "type": "error", "code": "ROOM_NOT_FOUND|FORBIDDEN|RATE_LIMITED", "client_message_id": "uuid|null" }
// 연결 유지
{ "type": "ping" } / { "type": "pong" }
```
- 토큰은 **쿼리 파라미터로 받지 않는다** (프록시/서버 로그 유출 방지)
- 참여자가 아닌 방으로 전송 → `error: FORBIDDEN` (연결은 유지)
- 메시지는 DB 저장 후 Redis pub/sub으로 전 인스턴스에 브로드캐스트
- 수신자 미접속 시 푸시(`chat_message`, 수신동의 `chat_push` 기준)
- 전송 제한: 사용자당 초당 5건
- 신고는 `/reports`(`target_type: chat_room`)로 통합 (§13)

---

## 12. 알림 (Notifications) [2차]

| Method | Path | Auth | 설명 |
|---|---|---|---|
| GET | `/notifications` | 필요 | 알림 목록 (페이지네이션) |
| POST | `/notifications/read` | 필요 | 읽음 처리 `{ "ids": ["uuid"] }` 또는 `{ "all": true }` |
| PUT | `/devices/{device_id}` | 필요 | 푸시 토큰 등록/갱신 `{ "push_token": "string", "platform": "ios\|android" }` |
| DELETE | `/devices/{device_id}` | 필요 | 푸시 토큰 해제 |

- 푸시 토큰은 경로가 아니라 **body**로 받는다 (경로는 `X-Device-Id`와 같은 device_id)
- 트리거:
  - `chat_message` — 새 채팅 (수신자 오프라인 시)
  - `interest_complex_new_transaction` — 관심 단지 신규 **적재** 거래 (ETL 후 1회 묶음 발송)
  - `interest_region_new_transaction` — 관심 법정동 신규 적재 거래 (ETL 후 1회 묶음 발송)
  - `agent_application_result` — 중개사 승인/반려
  - `listing_status_changed` — 찜한 매물 `sold` 전환 [3차]
- "신규 거래"는 **새로 적재된 거래**를 뜻한다 (계약일은 최대 30일 이전일 수 있음). 알림 문구에 계약일을 명시
- **광고성 푸시 수신동의 (정보통신망법)**
  - `PUT /users/me/consents`로 `marketing_push`, `marketing_night_push`(21~08시) 별도 관리
  - 동의·철회 일시를 기록, 동의 후 **2년마다 재동의** 요청 (`marketing_reconfirm_due`)
  - 광고성 메시지 제목에 `(광고)` 표기는 발송 서비스에서 강제

---

## 13. 신고 (Reports) [2차]

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/reports` | 필요 | 신고 생성 `{ "target_type": "listing\|chat_room\|user\|agent", "target_id", "reason", "detail" }` |
| GET | `/reports/me` | 필요 | 내 신고 처리 상태 |

- `reason`: `fake_listing|wrong_price|spam|abuse|illegal_brokerage|etc`
- 같은 사용자의 같은 대상 중복 신고는 `409 CONFLICT`
- **자동 비노출은 신고 건수가 아니라 신뢰도 가중 점수로 판단한다** (경쟁 중개사 신고 악용 방지)
  - 신고자 가중치: 가입 기간, 과거 신고 인용률, 동일 기기·동일 IP 대량 가입 여부
  - 점수가 임계치(정책 미정) 이상이면 관리자 큐에 **우선순위 상향**. 비노출은 원칙적으로 관리자 판단
  - 예외: `fake_listing`이 고신뢰 신고자 다수에게서 접수되면 **임시 비노출** 후 관리자 확정

---

## 14. 파일 업로드 (Uploads) [2차]

presigned URL 방식으로 확정.

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/uploads` | 필요 | 업로드 준비 → `file_id` + presigned PUT URL 발급 |
| POST | `/uploads/{file_id}/complete` | 필요(업로더) | 업로드 완료 확인 (크기·MIME 검증, 이미지 썸네일 생성 트리거) |
| GET | `/uploads/{file_id}` | 용도별 | 조회용 URL 반환 |

**`POST /uploads` 요청**
```json
{ "purpose": "listing_image|profile_image|agent_license_doc", "content_type": "image/jpeg", "size_bytes": 1048576 }
```
- `agent_license_doc` → `private/` prefix, **관리자만** 짧은 만료 서명 URL로 조회. 업로더 본인도 재조회 불가
- 이미지 → `public/` prefix, 공개 CDN URL
- 허용: 이미지 jpeg/png/webp 최대 10MB, 문서 pdf/jpeg/png 최대 10MB
- `complete`되지 않았거나 24시간 내 어떤 엔티티에도 연결되지 않은 파일은 배치로 삭제

---

## 15. 관리자 (Admin) [2차] — 내부용, `role=admin` 전용

| Method | Path | 설명 |
|---|---|---|
| GET | `/admin/agent-applications?status=pending` | 수동 심사 대기 목록 (자동 검증 실패 사유 포함) |
| POST | `/admin/agent-applications/{id}/approve` | 승인 |
| POST | `/admin/agent-applications/{id}/reject` | 반려 `{ "reason": "string" }` |
| GET | `/admin/reports?status=open` | 신고 큐 (신뢰도 점수 내림차순) |
| POST | `/admin/reports/{id}/resolve` | 처리 `{ "action": "dismiss\|hide_target\|restore_target\|ban_user" }` |
| GET | `/admin/audit-logs` | 관리자 조치 이력 |
| PUT | `/admin/glossary/{term_id}` | 용어 해설 등록/수정 |

- 모든 관리자 조치는 감사 로그(누가·언제·무엇을·사유)에 기록

---

## 16. 매물 (Listings) [3차] — 중개사가 등록하는 현재 판매중 매물

실거래가(과거 확정 거래)와 별개. **공인중개사법 제18조의2 및 국토교통부 고시(중개대상물 표시·광고 명시사항)** 항목을 필수로 강제한다.

| Method | Path | Auth | 설명 |
|---|---|---|---|
| GET | `/listings` | - | bbox + 필터 매물 목록 (페이지네이션) |
| GET | `/listings/{id}` | - | 매물 상세 (중개사무소 법정 표시 정보 포함) |
| POST | `/listings` | 중개사(승인) | 매물 등록 |
| PATCH | `/listings/{id}` | 중개사(본인) | 매물 수정 |
| DELETE | `/listings/{id}` | 중개사(본인) | 매물 삭제 (soft delete) |
| PATCH | `/listings/{id}/status` | 중개사(본인) | `active\|reserved\|sold` |
| POST | `/listings/{id}/renew` | 중개사(본인) | 노출 기간 연장 (매물 유효성 재확인) |
| GET | `/agents/me/listings` | 중개사(본인) | 내 매물 목록 (비활성·만료 포함) |

**`POST /listings` 요청**
```json
{
  "property_type": "apartment|officetel|villa|land",
  "deal_type": "sale|jeonse|monthly",
  "address": "string(지번 또는 도로명)",
  "unit_detail": "string(동·호, 비공개 저장)",
  "price": 0, "deposit": 0, "monthly_rent": 0,
  "exclusive_area_m2": 0.0, "supply_area_m2": 0.0,
  "floor": 0, "total_floors": 0,
  "approval_date": "YYYY-MM-DD(사용승인일)",
  "direction": "south|southeast|...", "direction_basis": "거실|안방 등 방향 기준",
  "room_count": 0, "bathroom_count": 0,
  "move_in_date": "YYYY-MM-DD|immediate|negotiable",
  "parking_count": 0,
  "maintenance_fee": { "monthly_total": 0, "includes": ["common", "water", "electricity", "gas", "internet", "tv"] },
  "land_category": "string(토지 전용: 지목)",
  "title": "string", "description": "string",
  "image_file_ids": ["uuid"],
  "options": { "elevator": true }
}
```
- **법정 필수 항목**: 소재지, 면적, 가격, 종류, 거래 형태 / 건축물: 총층수, 사용승인일, 방향(기준 포함), 방·욕실 수, 입주가능일, 주차대수, 관리비 / 토지: 지목
- 건축물 유형에서 위 항목 누락 → `400 VALIDATION_ERROR (field: 누락 항목)`
- 좌표는 서버가 `address` 지오코딩으로 산출. `unit_detail`(동·호)은 공개 응답에 포함하지 않음
- 거래유형별 가격 필드 규칙은 §0 표를 따른다
- 등록 후 **30일이 지나면 자동 만료**(비노출). `renew`로 재확인해야 연장 (허위·방치 매물 방지)
- 본인 매물이 아니면 `403 FORBIDDEN`

**`GET /listings/{id}` 응답 — 중개사무소 법정 표시 정보 포함**
```json
{
  "...매물 필드": "...",
  "agent": {
    "id": "uuid", "office_name": "string", "representative_name": "string",
    "brokerage_registration_no": "string", "office_address": "string", "office_phone": "string"
  },
  "status": "active", "expires_at": "iso8601", "view_count": 0, "created_at": "iso8601"
}
```
- `view_count`는 Redis 카운터 기반(근사치), 동일 디바이스 24시간 내 중복 조회는 1회로 집계

---

## 확정 필요한 정책

1. 신고 신뢰도 점수 산식과 임계치 (§13)
2. 채팅 메시지 보관 기간 (§11, §8 탈퇴)
3. 중개사 수동 심사 SLA (§9.1, 자동 검증 실패 건)
4. 줌 레벨별 집계 단위 경계값 (§2)
5. 매물 `sold` 전환 시 실거래가 자동 매칭 여부 (§16)
6. 안심번호(050) 전화 연결 도입 여부 (§9.2, 후순위)
