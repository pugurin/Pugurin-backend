# 관심 지역 주간 리포트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로그인한 회원이 법정동을 관심 지역으로 등록하면, 매주 월요일 지난주 소식을 묶은 리포트가 앱 안 알림함에 쌓인다.

**Architecture:** 백엔드가 `user_interests`에 지역 종류를 더하고, 주간 배치가 회원마다 리포트를 계산해 `notifications`에 저장한다(보낸 뒤 바뀌지 않음). 프론트는 지역 ☆, 관심 목록 지역 탭, 알림함·리포트 화면을 더한다. 리포트 문장은 서버가 만들어 저장하고 프론트는 그대로 보여 준다.

**Tech Stack:** 백엔드 FastAPI·SQLAlchemy(async, text SQL)·PostGIS·alembic·pytest. 프론트 Expo RN(web)·TypeScript·vitest.

**Spec:** `docs/INTEREST_REGION_REPORT.md` (같은 브랜치)

**Branches:** 백엔드 `feat/region-report`(이미 있음, #67 `feat/account-interests` 위). 프론트 `feat/region-report`를 `feat/account-interests`(#165) 위에 새로 만든다.

## Global Constraints

- 한국어 주석·문서·문구. 투자 권유성 문구 금지("오를 것", "추천", "지금이 기회").
- TDD: 실패하는 테스트 → 구현 → 통과 → 커밋. 커밋 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- 백엔드 테스트·린트: `backend/`에서 `pytest -q -p no:cacheprovider`, `ruff check app tests`, `ruff format app tests`. DB 테스트는 Docker PostGIS 필요(`open -a Docker`).
- 프론트: `npx vitest run`, `npx tsc --noEmit`. 프론트는 저장소가 prettier로 다 맞춰져 있지 않으니 **새 파일만** prettier를 돌린다(기존 파일 통째로 금지).
- `git commit -a` 금지. `.DS_Store`, `docs/AMENITY_CLUSTERS.md`는 커밋하지 않는다.
- 주 기간: KST 월 00:00 ~ 다음 월 00:00(반열린). 리포트 생성 월요일 08:00 KST.
- 한도: 관심 지역 10, 단지·경매 200. 알림 보관 90일. 지난주 비교는 양쪽 3건 이상일 때만. 새 거래는 계약일이 주 시작 60일 이내.
- 표시 포맷은 프론트와 같다: 억 이상 `9.8억`(소수 1자리, `.0` 생략), 그 아래 `4,100만`; 평당 `2,150만`(만 원 반올림); 변화 `+1.2%`/`-0.4%`/`0.0%`.

## Review Focus

1. 같은 주를 두 번 돌릴 때(cron 재시작·수동 실행) 리포트가 한 통만 생긴다 → Task 4 테스트 `test_rerun_keeps_one_report`.
2. 월요일 00:00 KST 바로 전·후에 끝난 수집 실행의 거래가 맞는 주에 들어간다 → Task 4 `test_week_boundary_uses_run_finished_at_kst`.
3. 백필이 그 주에 돌아 옛 거래가 대량으로 '처음 수집'돼도 리포트가 부풀지 않는다 → Task 4 `test_backfill_and_old_contracts_are_not_new`.
4. 탈퇴한 회원은 리포트를 받지 않고 알림도 지워진다 → Task 3 `test_withdraw_deletes_notifications`, Task 4 `test_withdrawn_users_get_nothing`.
5. 리포트를 보낸 뒤 지역 이름·대표점이 바뀌거나 관심 지역을 해제해도 받은 리포트는 그대로다 → Task 4 `test_report_is_a_snapshot`.

---

### Task 1: 관심 지역 등록 API (백엔드)

**Files:**

- Create: `backend/alembic/versions/0016_region_reports.py` (이 Task에선 `user_interests` CHECK만; Task 2·3이 같은 파일에 더한다)
- Modify: `backend/app/repositories/postgres/interests.py`, `backend/app/services/interest_service.py`, `backend/app/routers/interests.py`, `backend/app/schemas/interest.py`
- Test: `backend/tests/test_interest_api.py`

**Interfaces:**

- Produces: `InterestKind.REGION = "region"`; `InterestService.add_region(user_id: UUID, region_code: str) -> datetime`, `regions(user_id) -> list[InterestedRegion]`(`InterestedRegion(code, name, sigungu_name, lat, lng, added_at)`); `MAX_REGIONS = 10`; `InterestIdsOut.regions: list[str]`.

- [ ] **Step 1: 테스트** (`client()`·`sign_up()`은 기존 파일의 헬퍼. 테스트 DB의 동 코드는 `regions`에서 `level='dong'` 하나를 고르는 헬퍼 `dong_codes(url, n)`를 파일에 추가)
  - `test_region_interest_is_idempotent_and_listed`: PUT 두 번 → 둘 다 200, 같은 `added_at`; `GET /interests/regions` → `data[0]` = `{code, name, sigungu_name, lat, lng, added_at}`; `GET /interests` → `regions == [code]`.
  - `test_region_limit_is_10`: 동 11개째 PUT → 409 `CONFLICT`, 메시지에 "10개".
  - `test_unknown_or_sigungu_region_is_404`: `"2699999999"`·시군구 5자리(`"26230"`) → 404 `RESOURCE_NOT_FOUND`(라우터 패턴은 `^\d{5}|\d{10}$`로 받아서 5자리도 404가 되게).
  - `test_region_needs_login`: 토큰 없이 PUT/DELETE/GET → 401.
- [ ] **Step 2: 실패 확인** `pytest tests/test_interest_api.py -q -k region` → FAIL.
- [ ] **Step 3: 구현**
  - 0016 upgrade: `user_interests` CHECK를 `kind IN ('complex','auction','region')`로 바꾼다(제약 이름은 `\d+ user_interests`로 확인). downgrade는 region 행 삭제 후 원래 CHECK.
  - 한도는 종류별: `MAX_PER_KIND`(200)과 `MAX_REGIONS`(10) 중 종류에 맞는 값을 repo `add(max_items=)`에 넘긴다. 409 문구 "관심 지역은 10개까지 등록할 수 있어요".
  - `add_region`: `regions`에서 `code=:c AND level='dong'` 없으면 `not_found`.
  - `regions()`: `user_interests` ⨝ `regions`(동) ⨝ `regions`(시군구 = parent) 최근 등록 순, `centroid`의 lat/lng.
  - 라우터: `PUT/DELETE/GET /interests/regions[/{region_code}]`, `Cache-Control: private, no-cache`.
- [ ] **Step 4: 통과 확인** `pytest tests/test_interest_api.py -q` → PASS.
- [ ] **Step 5: 커밋** `feat(interests): 관심 지역(법정동) 등록`

### Task 2: 재개발 단계 변화·법정동 기록 (백엔드)

**Files:**

- Modify: `backend/alembic/versions/0016_region_reports.py`, `backend/app/ingestion/redevelopment.py` (`_UPSERT`, `_row`)
- Test: `backend/tests/test_redevelopment.py`

**Interfaces:**

- Produces: `redevelopments.previous_stage text`, `stage_changed_at timestamptz`, `region_code varchar(10)`.

- [ ] **Step 1: 테스트**
  - `test_stage_change_is_recorded_only_when_stage_differs`: 동기화 1회(단계 committee) → `previous_stage IS NULL, stage_changed_at IS NULL`; 같은 단계로 다시 → 그대로; association으로 바뀐 원천으로 다시(`now`+1일) → `previous_stage='committee'`, `stage_changed_at=그 now`.
  - `test_region_code_comes_from_the_lot_geocode`: FakeLookup 결과의 `b_code`가 `region_code`에 들어간다; 사무실 주소 구역은 `NULL`.
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현** 0016에 세 컬럼 추가. `_row`에 `region_code = geo.b_code if geo and geo.precise else None`. `_UPSERT` ON CONFLICT: `previous_stage = CASE WHEN redevelopments.stage IS DISTINCT FROM excluded.stage THEN redevelopments.stage ELSE redevelopments.previous_stage END`, `stage_changed_at`도 같은 조건으로 `:now`, `region_code`는 좌표 규칙(`keep_location`)과 같은 CASE를 따른다.
- [ ] **Step 4: 통과 확인** `pytest tests/test_redevelopment.py -q`.
- [ ] **Step 5: 커밋** `feat(redevelopment): 단계 변화 시각과 법정동 코드를 기록`

### Task 3: 알림 저장·알림함 API (백엔드)

**Files:**

- Modify: `backend/alembic/versions/0016_region_reports.py`, `backend/app/repositories/postgres/accounts.py`(`withdraw`), `backend/app/wiring.py`, `backend/app/main.py`(라우터 등록)
- Create: `backend/app/repositories/postgres/notifications.py`, `backend/app/services/notification_service.py`, `backend/app/routers/notifications.py`, `backend/app/schemas/notification.py`
- Test: `backend/tests/test_notification_api.py`, `backend/tests/test_common.py`(OpenAPI 경로 목록)

**Interfaces:**

- Produces: `PostgresNotificationRepository.insert_report(user_id, period_start: date, title, summary, payload: dict, now) -> bool`(새로 넣었으면 True, 이미 있으면 False); `list(user_id, offset, limit)`, `unread_count(user_id)`, `get(user_id, id)`, `mark_read(user_id, ids: list[UUID] | None, now)`, `delete_older_than(cutoff: datetime) -> int`.
- 응답 `NotificationOut`: `{id, kind, period_start, title, summary, read_at, created_at}`; 상세는 `+ payload`.

- [ ] **Step 1: 테스트** (테스트에서 알림은 repo `insert_report`로 직접 넣는다)
  - `test_list_is_newest_first_with_unread_count`: 3통 → 최신순, `meta.unread_count == 3`, `meta.total`.
  - `test_detail_and_read`: `GET /notifications/{id}` payload 포함; `POST /notifications/read {"ids":[id]}` → 204, unread 2; `{"all": true}` → 0.
  - `test_other_users_notification_is_404`.
  - `test_notifications_need_login`: 401.
  - `test_same_week_inserts_once`: `insert_report` 두 번 → True, False.
  - `test_withdraw_deletes_notifications`.
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현** 0016에 `notifications`(스펙 §5.3, `UNIQUE (user_id, kind, period_start)`, `CREATE INDEX ... (user_id, created_at DESC)`). `withdraw` 트랜잭션에 `DELETE FROM notifications WHERE user_id=:id`. 라우터 `GET /notifications?page&page_size(기본 20, 최대 50)`, `GET /notifications/{id}`, `POST /notifications/read`(본문 `ids`와 `all` 중 하나, 둘 다 없으면 400). 모두 `CurrentUser`, `Cache-Control: private, no-cache`.
- [ ] **Step 4: 통과 확인** `pytest tests/test_notification_api.py tests/test_common.py -q`.
- [ ] **Step 5: 커밋** `feat(notifications): 알림 저장과 알림함 API`

### Task 4: 주간 리포트 계산·생성 (백엔드)

**Files:**

- Create: `backend/app/services/region_report.py`
- Test: `backend/tests/test_region_report.py`

**Interfaces:**

- Consumes: Task 1 `user_interests`(region), Task 2 컬럼, Task 3 `insert_report`, `delete_older_than`.
- Produces: `week_start(now: datetime) -> date`(직전 완료 주의 월요일, KST); `async generate_region_reports(engine, *, week: date, now: datetime) -> ReportRun`(`ReportRun(users, created, skipped_empty, skipped_existing, no_collect)`); payload 모양:

  ```json
  {
    "period": { "start": "2026-11-17", "end": "2026-11-23" },
    "regions": [
      {
        "code": "2629010700",
        "name": "대연동",
        "sigungu_name": "남구",
        "lat": 35.13,
        "lng": 129.09,
        "empty": false,
        "lines": [
          "아파트 매매 12건 새로 신고 · 평당 중위 2,150만 (지난주보다 +1.2%)",
          "이번 주 가장 비싼 거래: 대연힐스테이트 34평형 9.8억 (계약 11/02)",
          "새 경매 2건 · 최저가 4.1억부터",
          "재개발: 대연8구역이 '관리처분인가'로 단계가 바뀌었어요"
        ],
        "sales": {
          "count": 12,
          "median_per_pyeong": 21500000,
          "change_pct": 1.2
        },
        "top_sale": {
          "complex_name": "...",
          "area_label": "34평형",
          "price": 980000000,
          "contract_date": "2026-11-02"
        },
        "auctions": { "count": 2, "min_price": 410000000 },
        "redevelopments": [
          { "area_name": "대연8구역", "stage": "disposal_approved" }
        ]
      }
    ],
    "sources": [
      "국토교통부 실거래가 공개시스템",
      "대법원 법원경매정보·온비드",
      "부산광역시 정비사업 정보"
    ],
    "lag_notice": "최근 30일 거래는 아직 신고되지 않았을 수 있어요"
  }
  ```

  `title = "관심 지역 주간 리포트 · 11/17~11/23"`, `summary = "대연동 거래 12건 · 새 경매 2건 외 2개 동네"`(소식 있는 첫 동네 기준, 나머지 동네 수). 소식 없는 동네 `lines = ["이번 주 새 소식이 없어요"]`, `empty: true`. 동네는 이름순.

- [ ] **Step 1: 테스트** (`tests/test_auction_api.py`의 `_populate`처럼 PostGIS에 직접 넣는 헬퍼: etl_runs·transactions·auctions·redevelopments·regions·users·user_interests)
  - `test_report_lines_for_a_busy_week`: 위 예시 숫자 그대로 나오는지(거래 12건 중 3건 이상 지난주 존재 → `+1.2%`).
  - `test_change_is_omitted_under_three_trades`: 이번 주 2건 → "(지난주보다 …)" 없음, `change_pct` null.
  - `test_week_boundary_uses_run_finished_at_kst`: run이 월 00:00 KST 1초 전에 끝남 → 이전 주, 1초 뒤 → 이번 주.
  - `test_backfill_and_old_contracts_are_not_new`: `job='backfill'` run이 처음 본 거래, 계약일 61일 전 거래 → 제외.
  - `test_cancelled_and_deleted_trades_are_excluded`.
  - `test_new_redevelopment_is_not_a_stage_change`: `previous_stage NULL` 구역 제외.
  - `test_all_quiet_sends_nothing`: 모든 동네 무소식 → 알림 0, `skipped_empty == 1`.
  - `test_rerun_keeps_one_report`: 두 번 실행 → 1통, 두 번째 `skipped_existing == 1`.
  - `test_no_collect_run_in_week_makes_nothing`: 그 주 `collect` 성공·partial run 없음 → 0통, `no_collect == True`.
  - `test_withdrawn_users_get_nothing`: `users.deleted_at` 있음 → 0통.
  - `test_report_is_a_snapshot`: 생성 뒤 `regions.name`·`centroid` 변경, 관심 지역 해제 → 저장된 payload 그대로.
  - `test_old_notifications_are_cleaned`: 91일 된 알림 삭제, 89일은 남음.
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현** 동네별 SQL 네 개(새 거래 집계·최고가·경매·재개발)를 `region_code = ANY(:codes)`로 한 번에 묶어 부른다. 평당가는 `app/repositories/postgres/market.py`의 `_PYEONG` 정의를 가져다 쓴다(복사 금지, import). 평형 표기는 공급평형을 알면 `round(공급평)평형`, 모르면 `round(전용평)평형`(`market.py`의 `_supply_pyeong` 재사용). 단계 이름은 `RedevelopmentStage` → 한국어 표(프론트 `REDEVELOPMENT_STAGE_LABEL`과 같은 말)를 이 모듈에 둔다.
- [ ] **Step 4: 통과 확인** `pytest tests/test_region_report.py -q`.
- [ ] **Step 5: 커밋** `feat(notifications): 관심 지역 주간 리포트 생성`

### Task 5: CLI·worker 주간 실행·문서 (백엔드)

**Files:**

- Modify: `backend/app/ingestion/__main__.py`, `backend/app/ingestion/worker.py`, `docs/API_SPEC.md`(§10 지역·알림), `docs/ERD.md`, `docs/FEATURE_SPEC.md`(F-INT-02, F-NOTI-02·03), `docs/DEPLOY.md`
- Test: `backend/tests/test_worker.py`(있으면 거기, 없으면 새로)

**Interfaces:**

- Consumes: Task 4 `generate_region_reports`, `week_start`.
- Produces: CLI `python -m app.ingestion region-reports [--week YYYY-MM-DD]`; `next_weekly_at(now, weekday=0, hour=8) -> datetime`.

- [ ] **Step 1: 테스트** `test_next_weekly_at`: 월 07:59 KST → 같은 날 08:00; 월 08:00 → 다음 주 월 08:00; 수요일 → 다음 월 08:00. `test_worker_runs_reports_on_monday`: `run_daily`처럼 시계·sleep을 주입한 주간 루프가 월 08:00에 리포트 job을 부른다.
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현** worker 프로세스에서 일일 루프와 주간 루프를 `asyncio.gather`로 함께 돌린다(한쪽 예외가 다른 쪽을 멈추지 않게 각 루프가 자기 예외를 잡는다, 기존 `run_daily`와 같은 방식). CLI는 결과(`ReportRun`)를 한 줄로 출력. 문서에 엔드포인트·표·배치 시각.
- [ ] **Step 4: 통과 확인** 전체 `pytest -q -p no:cacheprovider`, `ruff check app tests`.
- [ ] **Step 5: 커밋** `feat(notifications): 주간 리포트를 worker가 매주 월요일 만든다`

### Task 6: 프론트 API·관심 지역 상태

**Files:**

- Modify: `src/api/types.ts`, `src/api/repository.ts`, `src/state/interests.tsx`, `src/components/StarButton.tsx`, `src/api/fixtures.ts`(샘플 모드)
- Test: `tests/interests.test.tsx`, `tests/notifications.test.ts`(새)

**Interfaces:**

- Produces: `InterestKind = "complex" | "auction" | "region"`; `InterestIds.regions: string[]`; `RegionInterest = {code, name, sigungu_name, lat, lng, added_at}`; `AppNotification = {id, kind: "region_report", period_start, title, summary, read_at: string | null, created_at}`; `RegionReport = AppNotification & {payload: RegionReportPayload}`(백엔드 Task 4 payload 그대로 타입으로); `repo.regionInterests(signal)`, `repo.notifications(page, signal) -> Envelope<AppNotification[]> & meta.unread_count`, `repo.notification(id, signal)`, `repo.markNotificationsRead(ids | "all")`. `setInterest("region", code, on)`는 `/interests/regions/{code}`.

- [ ] **Step 1: 테스트** `setInterest("region")` 경로·메서드; `notifications()`가 Bearer와 `page`를 보내고 `unread_count`를 돌려준다; 샘플 모드 지역 10개 한도 409; 훅 `count.region`.
- [ ] **Step 2~4:** 실패 확인 → 구현 → `npx vitest run`, `npx tsc --noEmit`.
- [ ] **Step 5: 커밋** `feat(interests): 관심 지역과 알림 API 연결`

### Task 7: 지역 ☆과 관심 목록 지역 탭 (프론트)

**Files:**

- Modify: `src/screens/MarketSheets.tsx`(`RegionContent`), `src/screens/InterestsScreen.tsx`, `src/screens/MoreScreen.tsx`(관심 지역 줄), `App.tsx`(지역 탭에서 누르면 지도 이동)
- Test: `tests/interests.test.tsx`, `tests/market-sheets.test.tsx`(있는 파일에)

- [ ] **Step 1: 테스트** 동(10자리) 지역 시트에 "관심 지역 등록" 별이 있고 구(5자리)에는 없다; 관심 목록에 "지역" 탭, 줄을 누르면 `onRegion({lat,lng})`; 로그인 안 했으면 별을 눌러 로그인 안내.
- [ ] **Step 2~4:** 실패 → 구현(StarButton `kind="region"`, 라벨 "관심 지역 등록/해제") → 통과.
- [ ] **Step 5: 커밋** `feat(interests): 동네 시트 별과 관심 지역 탭`

### Task 8: 알림함·리포트 화면 (프론트)

**Files:**

- Create: `src/screens/NotificationsScreen.tsx`(목록·리포트 상세 두 화면), `src/state/notifications.tsx`(안 읽은 개수 훅)
- Modify: `src/screens/MoreScreen.tsx`("알림" 줄 + 배지), `App.tsx`(route `notifications`, `notification`)
- Test: `tests/notifications.test.tsx`

**Interfaces:**

- Consumes: Task 6 repo 메서드.
- Produces: `useUnreadCount(repo, account: string | null) -> number`(앱 켤 때·`AppState` active·로그인 바뀔 때 `notifications(1)`의 `meta.unread_count`).

- [ ] **Step 1: 테스트** 목록: 제목·요약·안 읽음 점, 빈 상태 "아직 받은 리포트가 없어요", 로그아웃이면 로그인 안내; 상세: 열면 `markNotificationsRead([id])`, 동네마다 `📍 {name}` 머리와 `lines`, `empty`면 한 줄, 출처·`lag_notice`; "지도에서 {name} 보기" → `onRegion({lat,lng})`; 더보기 배지 숫자.
- [ ] **Step 2~4:** 실패 → 구현 → 통과. 웹 미리보기(`.claude/launch.json`의 `web-interests-sample`)에서 샘플 리포트 화면을 스크린샷으로 확인.
- [ ] **Step 5: 커밋** `feat(notifications): 알림함과 주간 리포트 화면`

### Task 9: 개인정보처리방침 갱신 (프론트 `docs/privacy-policy` 브랜치, #166)

- [ ] `public/privacy.html` "서비스 이용 중 생성"에 관심 지역·알림(주간 리포트), 보유 기간에 "알림: 최대 90일". `tests/privacy-page.test.ts`에 "알림" 문구 확인 한 줄. 커밋 `docs(privacy): 관심 지역·알림 항목`.

### Task 10: 마무리

- [ ] 백엔드 전체 테스트·ruff, 프론트 전체 vitest·tsc.
- [ ] 백엔드 PR(base `feat/account-interests`), 프론트 PR(base `feat/account-interests`) — 푸시·PR은 사용자 확인 뒤.
