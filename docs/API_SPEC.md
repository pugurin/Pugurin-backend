# API 명세서 (Pugurin Backend) — v0.2

부산 부동산 지도 서비스 백엔드 API 명세입니다. 유저 여정(구매자/중개사/관리자)별로 빠짐없이 정리했습니다.
전체 개요는 [`../AGENTS.md`](../AGENTS.md), 백엔드 개발 규칙은 [`../backend/AGENTS.md`](../backend/AGENTS.md) 참고.

---

## 0. 공통 규약

**Base URL**: `/api/v1`

**인증**: `Authorization: Bearer <access_token>` (JWT, exp 30분) + refresh token(쿠키 또는 별도 저장, exp 14일)

**공통 응답 포맷 (성공)**
```json
{ "data": { }, "meta": { } }
```

**공통 에러 포맷**
```json
{ "error": { "code": "RESOURCE_NOT_FOUND", "message": "매물을 찾을 수 없습니다", "field": null } }
```

**표준 에러 코드**

| code | HTTP | 설명 |
|---|---|---|
| `VALIDATION_ERROR` | 400 | 요청 필드 검증 실패 (`field`에 실패한 필드명) |
| `UNAUTHORIZED` | 401 | 토큰 없음/만료 |
| `FORBIDDEN` | 403 | 권한 없음 (예: 일반 사용자가 중개사 전용 API 호출) |
| `RESOURCE_NOT_FOUND` | 404 | 대상 없음 |
| `CONFLICT` | 409 | 중복(이메일 중복 가입 등) |
| `RATE_LIMITED` | 429 | 요청 과다 |
| `INTERNAL_ERROR` | 500 | 서버 오류 |

**페이지네이션 (offset 방식)**
- 요청: `page`(기본 1), `page_size`(기본 20, 최대 100)
- 응답 `meta`: `{ "page": 1, "page_size": 20, "total": 134, "total_pages": 7 }`
- 예외: 채팅 메시지 히스토리는 `before_id` 커서 방식(§8)

**Rate limit**: 비로그인 조회 API는 IP당 분당 60회, 로그인 API는 사용자당 분당 120회. 초과 시 `RATE_LIMITED`.

**버전 관리**: URL 프리픽스(`/api/v1`)로 관리, breaking change는 `/api/v2` 신설.

---

## 1. 인증 / 계정 (Auth & Users)

사용자 유형은 `role`로 구분: `buyer`(일반), `agent`(공인중개사), `admin`.

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/auth/signup` | - | 일반 사용자 가입 |
| POST | `/auth/signup/agent` | - | 중개사 가입 신청 (승인 전까지 `pending` 상태) |
| POST | `/auth/login` | - | 로그인 |
| POST | `/auth/logout` | 필요 | refresh token 무효화 |
| POST | `/auth/refresh` | - (refresh token) | access token 재발급 |
| POST | `/auth/password/reset-request` | - | 이메일로 재설정 링크 발송 |
| POST | `/auth/password/reset` | - | 토큰으로 비밀번호 변경 |
| GET | `/users/me` | 필요 | 내 정보 |
| PATCH | `/users/me` | 필요 | 닉네임, 알림 설정 등 수정 |
| DELETE | `/users/me` | 필요 | 회원 탈퇴 (soft delete, 채팅/신고 이력은 보존) |

**`POST /auth/signup` 요청**
```json
{ "email": "string", "password": "string(8+, 영문+숫자)", "nickname": "string(2~12자)", "phone": "string(선택)" }
```
- 이메일 중복 → `409 CONFLICT`
- 응답: `access_token`, `refresh_token`, `user`

**`POST /auth/signup/agent` 요청** (파일은 `/uploads` 선등록 후 file_id 참조)
```json
{
  "email": "string", "password": "string", "office_name": "string",
  "business_registration_no": "string(사업자등록번호)",
  "license_no": "string(공인중개사 등록번호)",
  "license_doc_file_id": "uuid",
  "office_address": "string", "office_lat": 0.0, "office_lng": 0.0
}
```
- 가입 직후 `status: pending` — 로그인은 가능하지만 매물 등록/채팅 응답 등은 `403 FORBIDDEN`(승인 전) 처리
- 관리자 승인/거절은 §13 참고
- 승인/거절 시 푸시+이메일 알림 발송

**`GET /users/me` 응답**
```json
{
  "id": "uuid", "email": "string", "nickname": "string", "role": "buyer|agent|admin",
  "agent_status": "pending|approved|rejected|null",
  "notification_settings": { "chat": true, "price_alert": true, "marketing": false },
  "created_at": "iso8601"
}
```

---

## 2. 실거래가 (Transactions) — 공공데이터 기반, 조회 전용

읽기 전용 공개 데이터. 로그인 불필요.

| Method | Path | 설명 |
|---|---|---|
| GET | `/transactions` | bbox+필터 목록 (지도 마커) |
| GET | `/transactions/{id}` | 단건 상세 |
| GET | `/transactions/stats` | 지역/기간 통계(평단가, 추이) |
| GET | `/transactions/clusters` | 줌아웃 시 마커 클러스터링된 좌표+건수 (마커 과다 방지) |

**`GET /transactions` 쿼리**

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `bbox` | `min_lng,min_lat,max_lng,max_lat` | ✅ | 지도 뷰포트 |
| `property_type` | `apartment,officetel,villa,land` (콤마 다중선택) | - | 기본 전체 |
| `deal_type` | `sale,jeonse,monthly` | - | 기본 전체 |
| `period_from`, `period_to` | `YYYY-MM` | - | 미지정 시 기본 최근 12개월 |
| `price_min`, `price_max` | 원 단위 정수 | - | |
| `area_min`, `area_max` | ㎡ | - | |
| `sort` | `contract_date_desc\|price_asc\|price_desc` | - | 기본 `contract_date_desc` |

- `bbox` 누락 시 `400 VALIDATION_ERROR (field: bbox)`
- 응답 1건당 필드: `id, property_type, deal_type, address, dong, jibun, lat, lng, price, deposit(전세/월세용), monthly_rent, area_m2, floor, contract_date, building_name, build_year`
- **`/transactions/clusters`**: 서버사이드 grid clustering, 응답 `[{ "lat":.., "lng":.., "count": 42 }]` — 줌레벨 기준 임계치는 서버가 결정(클라이언트는 zoom만 전달)

**`GET /transactions/stats` 쿼리**: `region_code`(구/동 단위) 또는 `bbox`, `property_type`, `period_from/to`
- 응답: `{ "avg_price_per_m2": 0, "trend": [{"month":"2025-01","avg_price_per_m2":0}], "transaction_count": 0 }`

---

## 3. 매물 (Listings) — 중개사가 등록하는 현재 판매중 매물

실거래가(과거 확정 거래)와 별개로, 중개사가 지금 팔고 있는 매물.

| Method | Path | Auth | 설명 |
|---|---|---|---|
| GET | `/listings` | - | bbox+필터 매물 목록 |
| GET | `/listings/{id}` | - | 매물 상세 (담당 중개사 정보 포함) |
| POST | `/listings` | 중개사(승인됨) | 매물 등록 |
| PATCH | `/listings/{id}` | 중개사(본인) | 매물 수정 |
| DELETE | `/listings/{id}` | 중개사(본인) | 매물 삭제(soft delete) |
| PATCH | `/listings/{id}/status` | 중개사(본인) | `active\|reserved\|sold` 상태 변경 |
| GET | `/agents/me/listings` | 중개사(본인) | 내 등록 매물 목록(비활성 포함) |

**`POST /listings` 요청**
```json
{
  "property_type": "apartment|officetel|villa|land",
  "deal_type": "sale|jeonse|monthly",
  "address": "string", "lat": 0.0, "lng": 0.0,
  "price": 0, "deposit": 0, "monthly_rent": 0,
  "area_m2": 0.0, "floor": 0, "total_floors": 0, "build_year": 0,
  "title": "string", "description": "string",
  "image_file_ids": ["uuid"],
  "options": { "elevator": true, "parking": true }
}
```
- `deal_type=jeonse|monthly`면 `deposit` 필수, `monthly`면 `monthly_rent`도 필수 → 아니면 `VALIDATION_ERROR`
- 등록 즉시 지도에 노출 (별도 관리자 승인 없음 — 단, 신고 누적 시 자동 비노출, §11)
- 본인 소유 아닌 매물 수정/삭제 시도 → `403 FORBIDDEN`

목록 응답 필드는 §2 실거래가 필드 + `agent_id, agent_office_name, status, view_count, created_at`

---

## 4. 토지이용계획 (Land Use)

| Method | Path | 설명 |
|---|---|---|
| GET | `/land-use?lat=&lng=` | 좌표 기준 지번/용도지역/건폐율/용적률 (VWorld 온디맨드+캐시) |
| GET | `/land-use/{parcel_id}` | 지번 코드 직접 조회 |

응답: `{ "parcel_id", "jibun_address", "zone_type"(용도지역), "building_coverage_ratio", "floor_area_ratio", "use_restrictions": ["string"] }`
- VWorld 응답 캐시 TTL 24시간(자주 안 바뀌는 데이터)
- 캐시 미스 시 VWorld 호출 실패하면 `503`이 아니라 `{ "data": null, "meta": { "source_unavailable": true } }`로 graceful degrade (지도는 계속 떠야 하므로)

---

## 5. 용어 해설 (Glossary)

| Method | Path | 설명 |
|---|---|---|
| GET | `/glossary` | 전체 목록, `q` 검색 파라미터 |
| GET | `/glossary/{term_id}` | 단일 용어 |
| GET | `/glossary/related?context=` | 매물/토지 상세 화면에서 문맥상 관련 용어 자동 추천(예: `land_use` 화면이면 건폐율/용적률 우선) |

응답: `{ "term": "용적률", "short_definition": "40~50대도 이해할 쉬운 1줄 설명", "long_definition": "string", "example": "string" }`
- 타겟이 40~50대라 `short_definition`(툴팁용)과 `long_definition`(상세 화면용)을 분리

---

## 6. 중개사 (Agents, 조회 관점)

| Method | Path | Auth | 설명 |
|---|---|---|---|
| GET | `/agents?lat=&lng=&radius_km=` | - | 주변 중개사 목록(승인된 곳만) |
| GET | `/agents/{id}` | - | 프로필(사무소명, 연락처, 취급 매물 수, 응답률/응답 시간) |
| GET | `/agents/{id}/listings` | - | 해당 중개사 취급 매물 |
| PATCH | `/agents/me/profile` | 중개사(본인) | 사무소 소개, 사진, 취급 지역 수정 |

- `agent_status != approved`인 중개사는 공개 목록에서 제외

---

## 7. 즐겨찾기 / 최근 본 매물 (Favorites & Recently Viewed)

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/favorites/listings/{id}` | 필요 | 매물 찜 |
| DELETE | `/favorites/listings/{id}` | 필요 | 찜 해제 |
| GET | `/favorites/listings` | 필요 | 내 찜 목록 |
| POST | `/favorites/areas` | 필요 | 관심 지역 등록 (bbox 또는 행정동 코드 저장 → §9 알림과 연결) |
| GET | `/favorites/areas` | 필요 | 관심 지역 목록 |
| DELETE | `/favorites/areas/{id}` | 필요 | 관심 지역 삭제 |
| GET | `/history/recently-viewed` | 필요 | 최근 본 매물/실거래 (최대 50개, 자동 롤링) |

---

## 8. 채팅 (Chat)

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/chat/rooms` | 필요 | 채팅방 생성 (`{ "listing_id": "uuid" }` 또는 `{ "agent_id": "uuid" }`) |
| GET | `/chat/rooms` | 필요 | 내 채팅방 목록(마지막 메시지, 안읽음 수 포함) |
| GET | `/chat/rooms/{room_id}/messages` | 필요(참여자만) | 메시지 히스토리 (`before_id` 커서 페이지네이션) |
| POST | `/chat/rooms/{room_id}/read` | 필요 | 읽음 처리 |
| WS | `/ws/chat/{room_id}?token=` | 필요 | 실시간 송수신 (JWT는 쿼리 파라미터 또는 최초 프레임으로 전달) |
| POST | `/chat/rooms/{room_id}/report` | 필요 | 채팅방/상대방 신고 (§11로 연결) |

**WS 메시지 프로토콜**
```json
// 클라→서버
{ "type": "message", "content": "string" }
// 서버→클라
{ "type": "message", "id": "uuid", "sender_id": "uuid", "content": "string", "sent_at": "iso8601" }
{ "type": "read_receipt", "reader_id": "uuid", "last_read_message_id": "uuid" }
{ "type": "error", "code": "ROOM_NOT_FOUND" }
```
- 참여자가 아닌 room에 접속 시도 → WS 연결 자체를 4403으로 close
- 메시지는 REST로도 영속화(WS는 게이트웨이 역할만, `backend/AGENTS.md` 규칙과 일치)

---

## 9. 알림 (Notifications)

| Method | Path | Auth | 설명 |
|---|---|---|---|
| GET | `/notifications` | 필요 | 알림 목록(채팅, 관심지역 신규 실거래, 중개사 승인 결과 등) |
| POST | `/notifications/{id}/read` | 필요 | 읽음 처리 |
| POST | `/devices` | 필요 | 푸시 토큰 등록(FCM/APNs) |
| DELETE | `/devices/{token}` | 필요 | 로그아웃 시 토큰 해제 |

- 트리거 종류: `chat_message`, `favorite_area_new_transaction`, `agent_approval_result`, `listing_status_changed`(찜한 매물이 `sold`로 바뀜)
- 관심 지역(§7) 안에 신규 실거래 적재(ETL) 시 배치로 알림 생성 → ETL 잡과 알림 서비스 연동 필요

---

## 10. 파일 업로드 (Uploads)

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/uploads` | 필요 | 이미지/문서 업로드, `file_id` 반환 (presigned URL 방식 권장) |
| GET | `/uploads/{file_id}` | - (또는 서명 URL) | 파일 조회 |

- 용도: 매물 사진, 프로필 사진, 중개사 자격증 서류
- 서류(자격증)는 `access_level: private`로 저장, 관리자만 조회 가능 — 매물 사진과 동일 버킷/API로 취급하면 안 됨(권한 사고 위험) → **버킷 또는 prefix 분리 필수**

---

## 11. 신고 (Reports / Moderation)

| Method | Path | Auth | 설명 |
|---|---|---|---|
| POST | `/reports` | 필요 | 신고 생성 (`target_type: listing\|chat_room\|user`, `reason`, `detail`) |
| GET | `/reports/me` | 필요 | 내가 넣은 신고 처리 상태 조회 |

- 동일 대상 신고 누적 N건(정책 미정, 예: 3건) → 자동 비노출 + 관리자 큐 추가

---

## 12. 광고 (Ads)

| Method | Path | Auth | 설명 |
|---|---|---|---|
| GET | `/ads?slot=` | - | 슬롯별 노출 광고 조회 (`slot: map_banner\|listing_detail\|feed_native`) |
| POST | `/ads/{id}/impression` | - | 노출 로깅 |
| POST | `/ads/{id}/click` | - | 클릭 로깅 |

- 초기엔 노출 로직만, 과금/입찰은 범위 밖
- 광고 등록/관리는 공개 API가 아니라 §13 관리자 전용

---

## 13. 관리자 (Admin) — 내부용, `role=admin` 전용

| Method | Path | 설명 |
|---|---|---|
| GET | `/admin/agents?status=pending` | 중개사 가입 승인 대기 목록 |
| POST | `/admin/agents/{id}/approve` | 승인 |
| POST | `/admin/agents/{id}/reject` | 거절(사유 포함) |
| GET | `/admin/reports?status=open` | 미처리 신고 큐 |
| POST | `/admin/reports/{id}/resolve` | 처리(`action: dismiss\|hide_target\|ban_user`) |
| POST | `/admin/ads` | 광고 등록 |
| PATCH | `/admin/ads/{id}` | 광고 수정(활성/비활성) |

---

## 14. 헬스체크

| Method | Path | 설명 |
|---|---|---|
| GET | `/health` | DB/외부 어댑터 연결 상태 |

---

## 확정 필요한 정책 (구현 전 결정 필요)

1. 중개사 신고 누적 임계치 (몇 건에 자동 비노출?)
2. 관심지역 알림 배치 주기 (ETL 직후 즉시? 일 1회 배치?)
3. 매물 상태 `sold`로 바뀐 뒤 실거래가 테이블과 자동 매칭할지 여부
4. 중개사 승인 SLA (몇 영업일 내 처리?)
5. 채팅 메시지 보관 기간(무기한? N년 후 삭제?)
