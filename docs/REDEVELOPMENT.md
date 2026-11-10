# 정비사업(재개발·재건축) 구역 설계

작성일 2026-11-10 · 상태: 구현 완료(실호출 한 번으로 응답 구조·값 확인 후 보정)

## 무엇을 하나

부산시 공공데이터 오픈API "부산광역시_정비사업 정보"로 부산의 재개발·재건축·주거환경개선·소규모재건축 구역과 현재 사업추진단계를 매일 수집한다. 수집한 구역은 DB에 넣고 조회 API로 내보낸다. 프론트는 지도에 구역 점을 찍고, 단지·땅 화면에서 "근처 정비구역"으로 보여줄 수 있다.

## 원천

- 데이터: [부산광역시_정비사업 정보](https://www.data.go.kr/data/3069406/openapi.do)
  - 제공: 부산광역시 도시정비과
  - 이용허락범위 제한 없음, 무료
  - 포털 표기 갱신 주기는 "실시간"이다. 수정일 2025-07-29는 소개 페이지를 고친 날짜다.
- 요청: `GET https://apis.data.go.kr/6260000/MaintenanceBusinessStatus1/getMaintenanceBusiness1`
  - 파라미터: `serviceKey`, `pageNo`, `numOfRows`, `resultType=json`
- 응답 감싸기(실호출로 확인):
  `{"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_CODE"}, "body": {"totalCount": "351", "items": {"item": [...]}}}}`
  - `totalCount`와 숫자 항목이 모두 문자열로 온다.
  - 어댑터는 `item`이 객체 하나로 오거나 `items`가 빈 문자열·null인 경우도 견딘다. 해석은 `app/adapters/redevelopment.py`의 `parse_page` 한 곳에 모았다.
- 응답 항목(포털 명세 + 실호출):
  - 식별: `aCode`(예: "BARA_0000000000056"). **포털 명세에는 없지만 실제로 온다.** 실호출 351건이 모두 달랐다. 시 정비사업 통합홈페이지의 구역 페이지 주소(`https://dynamice.busan.go.kr/home/{aCode}/main.do`)이기도 하다.
  - 구역: `areaName`(구역명), `location`(위치 텍스트), `areaUnit`(구역면적㎡), `step`(사업추진단계, 예: "조합설립인가")
  - 규모: `scale`(예: "지하3층지상32층"), `wingNum`(동수), `generationJoo`(세대수), `gage`(용적률%), `longPyeyul`(건폐율%), `guildMemNum`(조합원수)
  - 관계자: `businessEntities`(사업주체), `telNo`, `phone`, `contractor`(시공자), `engineer`(감리자), `architect`(설계자), `specialists`(정비사업전문관리업체)
  - 이미지: `viewImgPath`(조감도), `loctImgPath`(위치도), `panoImgPath`(전경), `placeImgPath`(배치도). 실제 값은 `http://dynamice.busan.go.kr/data/busi_ara/BARA_….jpg` 꼴이다. 그런데 http 주소는 401을 돌려주고 https만 열려서(2026-11 확인) `https://`로 바꿔 저장한다.
  - 기타: `openInfoCnt`(입출금공개건수). 저장하지 않는다.
- 주의:
  - 사업 유형 필드가 없다. 유형은 `areaName`에서 읽는다(예: "… 소규모재건축", "… 재개발").
  - 좌표가 없다. `location`은 351건 중 76건이 비어 있고, 약 165건은 구역이 아니라 **조합 사무실 주소**다(예: "○○구 ○○로 12, 2층(○○동)").
  - 값이 문자열로 오고, "미선정"이나 빈 값이 섞인다.
  - 운영 데이터에 **테스트 행**이 섞여 있다(`areaName`에 "테스트"가 들어간 구역).
- 출처 표기: "부산광역시 정비사업 정보(공공데이터포털)".

## 설계 결정

- **키:** 설정 `redevelopment_api_key`(환경변수 `PUGURIN_REDEVELOPMENT_API_KEY`). 비어 있으면 수집을 건너뛰고 로그만 남긴다. 다른 수집은 계속 돈다.
- **수집:** 매일 worker 일과에 포함해 전체를 다시 받는다(`numOfRows=100`으로 `totalCount`까지 페이지를 넘김). 구역은 350건 안팎이라 하루 4회쯤 호출한다.
- **식별:** 구역 ID는 `aCode`다.
  - `aCode`가 비어 있으면 `NOCODE_` + `sha1(정규화한 areaName)` 앞 16자리로 만든다(실호출에서는 없었다).
  - 같은 실행에서 같은 ID가 두 번 나오면 뒤의 것을 버리고 리포트에 경고를 남긴다.
  - 응답의 `source_url`은 `aCode`로 만든 구역 페이지 주소다. `NOCODE_` 구역은 null이다.
- **테스트 행:** `areaName`에 "테스트"가 들어간 행은 저장하지 않고 리포트에 건수만 남긴다.
- **사라진 구역:** 이번 수집에 없는 구역은 지우지 않고 `closed_at`을 채운다. 다시 나타나면 비운다. 조회 기본값은 진행 중(`closed_at IS NULL`)만이다.
  - 수집이 실패하거나 (테스트 행을 뺀) 0건이면 아무것도 닫지 않는다. 일시 장애로 전체가 사라지지 않게 하기 위해서다. 이때 실행 상태는 `failed`라 `data_as_of`도 바뀌지 않는다.
- **유형 분류(`kind`):** `areaName`에 들어 있는 단어로 정한다. 긴 단어부터 검사하고, 일치하는 게 없으면 `other`다.
  - `small_reconstruction`: "소규모재건축"
  - `small_redevelopment`: "소규모재개발"
  - `street_housing`: "가로주택"
  - `reconstruction`: "재건축"
  - `redevelopment`: "재개발"
  - `residential_improvement`: "주거환경개선"
  - `urban_environment`: "도시환경"
  - 실호출 분포: 재개발 174, 재건축 68, 가로주택 56, 소규모재건축 49, 소규모재개발 4. 주거환경개선·도시환경은 없었지만 표에 남긴다.
- **단계 정규화(`stage`):** 원문 `step`은 그대로 `step_raw`에 둔다. 아래 표로 `stage`와 `stage_order`를 붙인다. 공백을 지우고 비교하며, 긴(구체적인) 말부터 검사한다. 그래서 "정비계획 수립 및 정비구역 지정"은 `designated`다. 표에 없는 값은 `stage=unknown`, `stage_order=null`이고, 수집 리포트에 모르는 단계 목록으로 남긴다.

  | stage             | stage_order | 원문에 포함되는 말     | 실호출 값(건수)                  |
  | ----------------- | ----------- | ---------------------- | -------------------------------- |
  | cancelled         | 0           | 해제, 취소             | 해제 3                           |
  | planned           | 1           | 예정구역, 기본계획     |                                  |
  | designated        | 2           | 정비구역지정, 정비계획 | 정비계획 수립 및 정비구역 지정 3 |
  | committee         | 3           | 추진위원회             | 추진위원회 구성 50               |
  | association       | 4           | 조합설립               | 조합설립인가 103                 |
  | review            | 5           | 건축심의, 통합심의     | 건축심의 및 통합심의 36          |
  | project_approved  | 6           | 사업시행               | 사업시행계획인가 46              |
  | disposal_approved | 7           | 관리처분               | 관리처분계획 22                  |
  | construction      | 8           | 착공, 이주, 철거       | 착공 24                          |
  | completed         | 9           | 준공                   | 준공 41                          |
  | transferred       | 10          | 이전고시               | 이전고시 18                      |
  | dissolved         | 11          | 조합해산, 청산         | 조합해산 5                       |

- **위치:** `location`을 세 가지로 나눠 `location_kind`에 둔다. 프론트가 "위치 없음" 이유를 설명할 수 있게 하기 위해서다.
  - `office`: 사무실 주소처럼 보이는 값. 괄호 안을 뺀 나머지에 `n층`, `n호`, "빌딩", "상가"가 있거나 도로명(`…로`/`…길` + 건물번호)이 있다. 지오코딩하지 않는다.
  - `lot`: 사무실이 아니고 `…동`/`…가`/`…리` + 지번(예: "가야동 410번지 일원(○○초교 남측 일원)", "사하구 감천1동 202일원")이 있는 값. 괄호, "번지", "일원"을 떼고 지번까지만 남겨 지오코딩한다.
  - `none`: 비었거나 둘 다 아닌 값. 지오코딩하지 않는다.
  - 지오코딩은 기존 카카오 지오코더로 `"부산광역시 " + 정리한 지번`을 주소 검색한다. 부산 결과가 있으면 `lat`, `lng`을 넣고, 지번까지 맞으면 `location_precise=true`, 법정동 중심점이면 `false`다. 없으면 좌표를 비운다(지도에는 안 보이고 목록에만 나온다).
  - 지오코딩 캐시(`GeocodeCache`)를 재사용해 매일 같은 주소를 다시 부르지 않는다.
  - 리포트에 `lot`/`office`/`none` 건수와 위치를 못 찾은 건수를 남긴다.
  - 실제 351건(2026-11 받은 것)에 돌려 본 결과: lot 95, office 169, none 87(시험 행 2건 포함 전체 기준). lot은 "부산시 동구 …"를 "부산광역시 동구 …"로, "대평동 1가"를 "대평동1가"로, "칠산동246"을 "칠산동 246"으로 고쳐서 검색한다.
- **숫자:** 면적, 세대수, 동수, 조합원수, 용적률, 건폐율은 숫자로 바꾸고, 바꿀 수 없으면 null이다. 쉼표와 단위(㎡, %, 세대, 동, 명)는 떼고 읽는다. "미선정", "미정", "-", 빈 문자열은 null이다(글자 항목도 마찬가지). "1,174(임대 60)"처럼 괄호 설명이나 "261%이하"처럼 "이하"가 뒤에 붙으면 앞의 숫자를 읽는다.
- **연락처:** `tel`은 `telNo`만 쓴다. `phone`은 개인 휴대전화일 수 있어 저장하지 않는다.

## DB (alembic `0010_redevelopments`)

`redevelopments`

| 컬럼                                                               | 타입                        | 비고                                 |
| ------------------------------------------------------------------ | --------------------------- | ------------------------------------ |
| id                                                                 | text PK                     | `aCode` (없으면 `NOCODE_…`)          |
| area_name                                                          | text not null               |                                      |
| kind                                                               | text not null               | 위 분류                              |
| step_raw                                                           | text                        |                                      |
| stage                                                              | text not null               | 위 표, 기본 unknown                  |
| stage_order                                                        | smallint                    |                                      |
| location                                                           | text                        | 원문                                 |
| location_kind                                                      | text not null default none  | lot / office / none                  |
| lat, lng                                                           | double                      |                                      |
| location_precise                                                   | bool not null default false |                                      |
| geom                                                               | geometry(Point, 4326)       | lat/lng가 있을 때. GIST 인덱스       |
| area_m2                                                            | double                      |                                      |
| households, buildings, union_members                               | integer                     |                                      |
| floor_area_ratio, building_coverage_ratio                          | double                      | 용적률, 건폐율                       |
| scale                                                              | text                        |                                      |
| business_entity, contractor, engineer, architect, specialists, tel | text                        |                                      |
| images                                                             | jsonb                       | `{view, location, panorama, layout}` |
| first_seen_at, last_seen_at                                        | timestamptz not null        |                                      |
| closed_at                                                          | timestamptz                 |                                      |

메타는 기존 수집과 같은 방식이다: `etl_runs`에 `job='redevelopment'`로 실행을 남기고, 마지막으로 끝난(`success`·`partial`·`quota_exceeded`) 실행의 `finished_at`이 `data_as_of`다.

## API

- `GET /redevelopments`
  - 쿼리: `bbox`(선택), `kind`(쉼표 구분), `stage`(쉼표 구분), `include_closed`(기본 false), `page`, `page_size`(최대 500)
  - 정렬: `stage_order DESC NULLS LAST, area_name`(같으면 id)
  - 응답: `data[]`(위 컬럼 중 공개 항목, id·`source_url`·`area_pyeong`·`is_closed` 포함), `meta`(`data_as_of`, `source_attribution`, `page`, `page_size`, `total`, `total_pages`)
- `GET /redevelopments/{id}`: 단건, 404는 기존 오류 형식을 따른다.
- 수집이 한 번도 끝나지 않았으면 다른 데이터처럼 503 `DATA_NOT_READY`를 준다.
- 요청 한도, 캐시, 오류 형식은 기존 라우터와 같다.

## 하지 않는 것

- 정비구역 경계(폴리곤): 원천에 없다.
- 단계별 인가 날짜: 원천에 없다.
- 단지(complex)와의 자동 연결: 다음 단계다. 이번에는 좌표만 둔다.
- 프론트 화면: 이 PR 범위가 아니다.

## 확인 필요(실데이터)

- 모르는 단계가 나오면(수집 리포트의 `unknown_steps`) 표를 보강한다.
- `location`이 `office`/`none`인 구역(약 절반)은 지도에 안 나온다. 위치도 이미지나 구역 페이지에서 지번을 따로 얻을 방법을 찾는다.
- "실시간" 표기가 맞는지 정비사업 통합홈페이지(dynamice.busan.go.kr)의 구역 페이지와 몇 건 대조한다.
