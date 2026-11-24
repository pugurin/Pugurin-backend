# 관심 단지 주간 리포트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 동네 단위로 만든 주간 리포트(백엔드 PR #69, 프론트 `feat/region-report`)를 관심 단지 단위로 고친다.

**Architecture:** 알림 저장·API, 주간 워커, 스냅샷 규칙은 그대로 두고, 관심 지역을 걷어낸 뒤 리포트 계산을 단지 기준으로 다시 쓴다. 리포트 받기 설정은 회원 설정 jsonb의 키 하나다. 프론트는 지역 ☆·지역 탭을 걷어내고 리포트 화면을 새 payload로 다시 그린다.

**Tech Stack:** 백엔드 FastAPI · async SQLAlchemy(text SQL) · PostGIS · alembic · pytest(실제 PostGIS). 프론트 Expo RN web · TypeScript · vitest.

**Spec:** `docs/INTEREST_COMPLEX_REPORT.md` (백엔드 저장소)

## Global Constraints

- 모든 주석·문서·커밋 메시지는 한국어. TDD: 실패하는 테스트 → 구현 → 통과.
- 커밋 메시지 끝: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. `git commit -a` 금지, 파일을 지정해 `git add`. `.DS_Store`, `docs/AMENITY_CLUSTERS.md`는 스테이징하지 않는다. 푸시하지 않는다.
- 프론트 기존 파일에 prettier 전체 실행 금지(새 파일만).
- 백엔드 저장소 `/Users/owl/pugurin/Pugurin-backend`(코드는 `backend/`), 브랜치 `feat/region-report`. 프론트 저장소 `/Users/owl/pugurin/Pugurin-frontend`, 브랜치 `feat/region-report`. 둘 다 기준(base)은 `origin/feat/account-interests`.
- 백엔드 테스트: `cd backend && PATH=/private/tmp/claude-501/-Users-owl-pugurin/c19cdd34-6a9e-4dfc-ac20-d192b465ed06/scratchpad/venv312/bin:$PATH pytest <경로> -q`. 린트 `ruff check app tests`.
- 프론트 테스트: `npx vitest run <경로>`, 타입 `npx tsc --noEmit`. 포트 8081은 건드리지 않는다.
- 0016 마이그레이션은 새 파일 없이 그 자리에서 고친다(아직 배포 전).
- 알림 종류 `interest_report`, CLI `interest-reports`, 설정 키 `weekly_report`(기본 true).
- 투자 권유성 문구 금지. 사실만 쓴다.
- 표기: 가격은 기존 `format_price`(억·만), 평형은 "N평형"(공급평형 있으면 공급평형, 없으면 전용평 — `market.py`의 `_supply_pyeong`·`_PYEONG`과 같은 규칙), 정비사업 단계 이름은 기존 `STAGE_LABEL`.

## Review Focus

- 한 단지가 두 회원의 관심이면 두 리포트 모두 같은 숫자여야 한다(단지별 계산을 회원 사이에 공유).
- 한 경매가 같은 `pnu`의 두 관심 단지에 둘 다 붙으면 안 된다(⚖ 배지와 같은 잇기, 한 단지에만).
- 단지가 정비구역 조각 여러 개에 걸쳐도 같은 구역 단계 변화는 한 번.
- 설정 키가 없는 회원(옛 회원)은 받는다(기본 true). `false`로 끈 회원만 뺀다.
- 지난 브랜치의 동네 관련 코드가 남아 `GET /interests`가 `regions`를 내거나, 프론트에 지역 ☆이 남지 않는다.

## Payload (백엔드가 저장, 프론트가 그린다 — 두 쪽이 이 모양을 지킨다)

```jsonc
{
  "period": { "start": "2026-11-16", "end": "2026-11-22" },
  "headline": "관심 단지 4곳 중 2곳에 새 소식이 있어요",
  "total": 4,
  "with_news": 2,
  "complexes": [
    // 소식 있는 단지만, 이름순
    {
      "id": "uuid",
      "name": "A단지",
      "sigungu_name": "남구",
      "dong_name": "대연동", // 모르면 null
      "lat": 35.1,
      "lng": 129.1, // 모르면 null
      "lines": [
        "새 실거래 5건",
        "34평형 2건: 최근 9.4억 (11/02) · 직전 같은 평형보다 +2,000만",
        "...",
      ],
      "sales": {
        "count": 5,
        "areas": [
          // 최대 3개, 건수 많은 순 → 같으면 area_bucket 큰 순
          {
            "area_bucket": 85,
            "area_label": "34평형",
            "count": 2,
            "latest": { "price": 940000000, "contract_date": "2026-11-02" },
            "prev_diff": 20000000,
          }, // 없으면 null
        ],
        "more_areas": 1, // 3개를 넘은 평형 수(없으면 0)
      },
      "auctions": [
        {
          "id": "...",
          "area_label": "25평형",
          "min_price": 410000000,
          "date": "2026-12-10",
          "source": "court",
        },
      ], // area_label·min_price·date는 null 가능
      "redevelopments": [
        {
          "id": "...",
          "area_name": "대연8구역",
          "kind": "reconstruction",
          "previous_stage": "association",
          "stage": "project_approved",
        },
      ],
    },
  ],
  "quiet": [{ "id": "uuid", "name": "C단지" }], // 소식 없는 단지, 이름순
  "sources": [
    "국토교통부 실거래가 공개시스템",
    "대법원 법원경매정보·온비드",
    "부산광역시 정비사업 정보",
  ],
  "lag_notice": "최근 30일 거래는 아직 신고되지 않았을 수 있어요",
}
```

- `lines`는 화면에 그대로 쓰는 문장이다(푸시·이메일용). 순서: `새 실거래 N건` → 평형 줄들(`{area_label} {count}건: 최근 {가격} ({MM/DD})[ · 직전 같은 평형보다 {+/-금액}]`, 1건이면 `{area_label} 1건: {가격} ({MM/DD})[ · …]`) → `외 N개 평형`(있을 때) → 경매 줄(`새 경매 N건: {area_label} · 최저가 {가격} · 매각기일 {MM/DD}` — 건이 여럿이면 건마다 한 줄, 공매면 `입찰 마감`, 모르는 값은 그 조각을 뺀다) → 정비사업 줄(`{area_name}: '{이전}' → '{지금}'으로 단계가 바뀌었어요`, 이전을 모르면 `'{지금}' 단계가 됐어요`).
- 금액 차이 표기: `+2,000만`, `-1.2억`(억 이상은 `format_price` 규칙, 부호 붙임). 0이면 `직전 같은 평형과 같아요`.
- 제목 `관심 단지 주간 리포트 · {M/D}~{M/D}`. 요약 `{첫 단지 이름} {첫 소식}[ 외 N개 단지]`, 첫 소식은 `새 실거래 N건` > `새 경매 N건` > `{재개발|재건축|…} 단계 변경` 순(종류 이름은 기존 kind 표기).

---

### Task 1: 동네 관련 코드 걷어내기 (백엔드)

**Files:**

- Modify: `backend/alembic/versions/0016_region_reports.py` → 이름을 `0016_interest_reports.py`로(`revision` id 문자열은 그대로)
- Restore to base: `app/repositories/postgres/interests.py`, `app/routers/interests.py`, `app/schemas/interest.py`, `app/services/interest_service.py`, `tests/test_interest_api.py` (`git checkout origin/feat/account-interests -- <파일>`)
- Modify: `app/ingestion/redevelopment.py`(`region_code`와 그 지오코딩 값 전달 제거, `previous_stage`/`stage_changed_at`은 유지), `tests/test_redevelopment.py`
- Modify: `app/repositories/postgres/accounts.py`(탈퇴 시 `notifications` 삭제는 유지, 관심 지역 관련만 정리)

**Interfaces:**

- Produces: 0016 = `redevelopments.previous_stage text`, `stage_changed_at timestamptz` + `notifications`(`kind` CHECK `IN ('interest_report')`). `user_interests` CHECK는 0015 그대로.

- [ ] Step 1: `tests/test_redevelopment.py`에서 `region_code` 단언을 지우고, 단계가 바뀌면 `previous_stage`·`stage_changed_at`이 채워지고 새 구역은 비어 있는 테스트가 남아 있는지 확인한다. `tests/test_interest_api.py`에 `PUT /interests/regions/2626010500`이 404(라우트 없음)이고 `GET /interests` 응답에 `regions` 키가 없다는 테스트를 더한다.
- [ ] Step 2: 실행해 새 테스트가 실패하는지 본다.
- [ ] Step 3: 위 파일들을 고친다. 0016에서 `_interest_kind_region_*`와 `region_code`를 빼고 `kind`를 `interest_report`로. `app/services/region_report.py`와 `tests/test_region_report.py`는 이 태스크에서 아직 지우지 않는다(Task 3이 바꾼다) — 다만 import가 깨지면 깨진 테스트 파일만 `pytest.mark.skip`으로 막지 말고, 필요한 최소 수정으로 import만 살린다.
- [ ] Step 4: `pytest tests/test_interest_api.py tests/test_redevelopment.py tests/test_notification_api.py -q`, `ruff check app tests` 통과.
- [ ] Step 5: 커밋 `refactor: 관심 지역을 걷어내고 알림 종류를 interest_report로 바꾼다`.

### Task 2: 리포트 받기 설정 (백엔드)

**Files:**

- Modify: `app/schemas/auth.py`(`DEFAULT_SETTINGS`에 `"weekly_report": True`, `SettingsPatch`·`UserSettingsOut`에 `weekly_report`)
- Modify: `app/routers/notifications.py`, `app/services/notification_service.py`, `app/schemas/notification.py`
- Test: `tests/test_notification_api.py`

**Interfaces:**

- Produces: `GET /notifications/settings` → `{"data": {"weekly_report": bool}}`, `PUT /notifications/settings` 본문 `{"weekly_report": bool}`(extra 금지) → 같은 응답. 저장은 `users.settings`의 `weekly_report` 키(기존 `AccountRepository.update(user_id, nickname=None, settings={...})` 재사용). 응답 헤더 `Cache-Control: private, no-cache`. 토큰 없으면 401.
- 배치가 쓸 SQL 조건: `coalesce((u.settings->>'weekly_report')::boolean, true)`.

- [ ] Step 1: 테스트 — 새 회원 GET은 `true`; PUT false 뒤 GET은 `false`이고 `GET /users/me`의 `settings.weekly_report`도 `false`; `PATCH /users/me {"settings": {"weekly_report": true}}` 뒤 GET은 `true`; 토큰 없이 401; 본문에 다른 키면 422.
- [ ] Step 2: 실패 확인. Step 3: 구현. Step 4: `pytest tests/test_notification_api.py tests/test_auth*.py -q` 통과.
- [ ] Step 5: 커밋 `feat: 주간 리포트 받기 설정`.

### Task 3: 단지 기준 리포트 계산 (백엔드)

**Files:**

- Rename+rewrite: `app/services/region_report.py` → `app/services/interest_report.py`
- Rename+rewrite: `tests/test_region_report.py` → `tests/test_interest_report.py`

**Interfaces:**

- Consumes: Task 1의 `notifications`(`kind='interest_report'`), Task 2의 설정 조건, 기존 `PostgresNotificationRepository.insert_report(user_id, week, title, summary, payload, now)`(종류 상수만 `interest_report`로) · `delete_older_than`.
- Produces: `week_start(now) -> date`(그대로), `generate_interest_reports(engine, *, week: date, now: datetime) -> ReportRun`. `ReportRun(users, created, skipped_empty, skipped_existing, no_collect)` 의미는 그대로(관심 단지 기준). 상수 `RETENTION`, `NEW_CONTRACT_DAYS=60`, `SOURCES`, `LAG_NOTICE`, `STAGE_LABEL`, `format_price` 유지. `MAX_AREA_LINES = 3`, `PREV_TRADE_MAX_DAYS = 365` 추가. `MIN_COMPARE_TRADES`·평당 중위·지난주 대비는 지운다.
- 대상: `user_interests.kind='complex'` + `users.deleted_at IS NULL` + 설정 켜짐. 탈퇴 경합 처리(기존 `EXISTS … FOR SHARE`)는 그대로.
- 계산은 관심 단지 id 전체에 대해 한 번 하고 회원마다 나눠 쓴다.
- 경매 잇기: `repositories/postgres/auctions.py`의 `_FROM` 안 `cx` LATERAL과 같은 규칙(같은 `pnu` 단지 중 같은 유형·전용면적이 `:area_tolerance` 안에서 가장 가까운 평형이 있는 곳, 그다음 `c.id`)으로 경매 → 단지 하나. 그 SQL 조각을 함수나 상수로 꺼내 두 곳이 같이 쓰게 한다(복사 금지). 경매 평형 표기는 이어진 평형(`area_bucket`·`supply_area_pyeong`)으로.
- 정비사업: `ST_Covers(z.geom, c.location)` + `z.redevelopment_id = d.id` + `d.stage_changed_at` 주 안, `DISTINCT (c.id, d.id)`.
- 직전 같은 평형 거래: 같은 단지·같은 `floor(exclusive_area_m2+0.5)`, 이번 주 새 거래가 아닌 것, 해제·삭제·0원 제외, `contract_date <= 최근 거래 계약일`, `contract_date >= 최근 거래 계약일 - 365일`, 계약일 늦은 순 → id 순 첫 건.

- [ ] Step 1: 테스트(실제 PostGIS, 기존 `test_region_report.py`의 픽스처 헬퍼를 옮겨 쓴다). 필요한 것:
  - 주 경계(월 00:00 KST 직전·직후 수집), 처음 수집 기준(계약일이 옛날이어도 이번 주 수집이면 포함), 60일 넘은 계약 제외, 해제·삭제·0원 제외
  - 평형 묶음: 4개 평형 거래 → `areas` 3개(건수 순, 같으면 큰 평형 먼저) + `more_areas == 1` + `lines`에 `외 1개 평형`
  - 공급평형 없으면 전용평 표기
  - `prev_diff`: 직전 거래 있을 때 차이, 없을 때 null, 366일 전이면 null, 직전 거래가 이번 주 새 거래면 쓰지 않음
  - 경매: 같은 pnu에 관심 단지 둘(평형이 다른) → 경매는 면적 맞는 한 단지에만; 닫힌 경매도 포함; 공매는 `bid_end_at` 날짜
  - 정비사업: 경계 조각 두 개에 걸친 단지 → 한 번; 경계 밖 단지 → 없음; `stage_changed_at` 없는 새 구역 → 없음
  - 요약 우선순위(경매만 있는 단지가 첫 단지면 `새 경매 N건`)·`외 N개 단지`·제목 형식·`headline`
  - 모든 단지 무소식 → 안 만듦(`skipped_empty`); 같은 주 두 번 → 한 통; 수집 없던 주 → `no_collect`; 설정 false 회원 제외, 키 없는 회원 포함; 탈퇴 회원 제외; 두 회원이 같은 단지면 같은 블록
  - 끝나지 않은 주·월요일 아닌 주 → ValueError(기존 그대로)
- [ ] Step 2: 실패 확인. Step 3: 구현. Step 4: `pytest tests/test_interest_report.py -q`, `ruff check app tests` 통과.
- [ ] Step 5: 커밋 `feat: 관심 단지 기준 주간 리포트 계산`.

### Task 4: CLI·워커 이름과 문서 (백엔드)

**Files:**

- Modify: `app/ingestion/__main__.py`(`region-reports` → `interest-reports`, 같은 `--week`), `app/ingestion/worker.py`(호출 함수 이름), `tests/test_worker.py`
- Modify: `docs/API_SPEC.md`, `docs/ERD.md`, `docs/FEATURE_SPEC.md`, `docs/DEPLOY.md` — 관심 지역·`region_code`·`region_report` 언급을 단지 리포트·설정으로. `INTEREST_REGION_REPORT.md` 링크는 `INTEREST_COMPLEX_REPORT.md`로.
- Delete: `docs/superpowers/plans/2026-11-23-interest-region-report.md`

- [ ] Step 1: `tests/test_worker.py`의 주간 job 테스트가 `generate_interest_reports`를 부르도록 바꾸고, CLI `interest-reports --week 2026-11-16`이 그 함수를 그 주로 부르는 테스트(기존 CLI 테스트 방식)를 맞춘다. Step 2: 실패 확인. Step 3: 구현·문서.
- [ ] Step 4: 전체 `pytest -q`(약 6분, 백그라운드로) 통과, `ruff check app tests`, `grep -rn "region_report\|region-reports\|interests/regions" app tests ../docs` 결과 없음(이 계획 문서와 설계 문서의 "바뀐 이유" 줄 제외).
- [ ] Step 5: 커밋 `refactor: 리포트 CLI·워커 이름과 문서를 관심 단지 기준으로`.

### Task 5: 지역 걷어내기와 새 타입 (프론트)

**Files:**

- Restore to base: `src/components/StarButton.tsx`, `src/screens/MarketSheets.tsx`, `src/screens/InterestsScreen.tsx`, `src/state/interests.tsx`, `tests/interests.test.tsx`, `tests/region-mode.test.tsx`(base에 없으면 이 브랜치에서 지역 ☆ 부분만 되돌림)
- Modify: `src/api/types.ts`, `src/api/repository.ts`, `src/api/client.ts`, `src/api/fixtures.ts`, `App.tsx`(관심 지역 줄 `showRegion` 연결 제거), `tests/notifications.test.ts`

**Interfaces:**

- Produces(types.ts): `InterestIds = { complexes: string[]; auctions: string[] }`, `InterestKind = "complex" | "auction"`, `AppNotification.kind: "interest_report"`, `InterestReportPayload`(위 Payload 그대로, 필드 이름 그대로), `InterestReport = AppNotification & { payload: InterestReportPayload }`, `ReportSetting = { weekly_report: boolean }`.
- Produces(repository): 기존 알림 메서드 유지 + `reportSetting(): Promise<ReportSetting>`, `setReportSetting(on: boolean): Promise<ReportSetting>`. 샘플 모드는 메모리에 저장(기본 true).
- 샘플 픽스처: 단지 리포트 2통(소식 있는 단지 2곳 + 조용한 단지 2곳인 것, 경매·정비사업 줄이 있는 것), 하나는 안 읽음.

- [ ] Step 1: 테스트 — API 모드에서 `reportSetting`은 `GET /notifications/settings`, `setReportSetting(false)`는 `PUT /notifications/settings` 본문 `{"weekly_report": false}`; 샘플 모드에서 끄면 다시 읽어도 꺼져 있음; `InterestsScreen`에 "지역" 탭이 없음; 지역 시트에 관심 ☆이 없음.
- [ ] Step 2: 실패 확인. Step 3: 구현. Step 4: `npx vitest run`, `npx tsc --noEmit` 통과(리포트 화면 테스트가 깨지면 Task 6에서 고칠 것이므로 그 파일만 실패 허용 — 보고서에 적는다).
- [ ] Step 5: 커밋 `refactor: 관심 지역을 걷어내고 단지 리포트 타입·설정 API를 더한다`.

### Task 6: 단지 리포트 화면과 설정 스위치 (프론트)

**Files:**

- Modify: `src/screens/NotificationsScreen.tsx`, `App.tsx`, `tests/notifications.test.tsx`

**Interfaces:**

- Consumes: Task 5의 타입·저장소 메서드, `App.tsx`의 기존 `openComplex(complexId)`, 관심 목록 라우트 `{ kind: "interests", tab: "complex" }`.
- `ReportScreen` props: `repo`, `id`, `onBack`, `onComplex(id: string)`, `onCompare()`, `onRead`. `onRegion`은 없앤다.
- `NotificationsScreen` 위에 "주간 리포트 받기" 스위치(로그인했을 때만). 바꾸면 `setReportSetting`, 실패하면 되돌리고 토스트 `설정을 바꾸지 못했어요`.

- [ ] Step 1: 테스트 — 리포트 화면이 `headline`, 단지 이름(+`시군구 동`), `lines` 순서대로, 조용한 단지 한 줄(`C단지 · D단지: 이번 주 새 소식이 없어요`), 출처·지연 안내를 보여 줌; 단지 이름 누르면 `onComplex(id)`; "관심 단지 비교하러 가기" 누르면 `onCompare`; 스위치 끄면 `setReportSetting(false)` 호출, 실패 시 되돌림; 로그인 안 했으면 스위치 없음.
- [ ] Step 2: 실패 확인. Step 3: 구현(`App.tsx`: `onComplex={openComplex}`, `onCompare={() => setRoute({ kind: "interests", tab: "complex" })}`).
- [ ] Step 4: `npx vitest run`, `npx tsc --noEmit` 전부 통과.
- [ ] Step 5: 커밋 `feat: 관심 단지 리포트 화면과 받기 설정`.

### Task 7: 개인정보처리방침 고치기 (프론트, 브랜치 `docs/privacy-policy`)

**Files:**

- Modify: `public/privacy.html`, `tests/privacy-page.test.ts`

- [ ] Step 1: 아직 푸시 안 한 커밋 `0d7e217`이 더한 "관심 지역" 문구를 테스트로 확인하고, 테스트를 "관심 지역"이 없고 "알림(90일 보관)"과 "주간 리포트 받기 설정"이 있다는 단언으로 바꾼다.
- [ ] Step 2: 실패 확인. Step 3: 문구 수정. Step 4: `npx vitest run tests/privacy-page.test.ts` 통과.
- [ ] Step 5: 커밋 `docs(privacy): 관심 지역을 빼고 알림·리포트 설정을 적는다`. 끝나면 `git checkout feat/region-report`로 돌아온다.
