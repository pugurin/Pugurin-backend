# 로그인·회원가입 설계 (F-AUTH-01~05)

기능명세 `F-AUTH-01~05`와 API 명세 §8을 구현하는 설계다. 프론트 화면(Pugurin-frontend #138)과 그 요청서 `docs/BACKEND_AUTH_REQUEST.md`를 반영해 명세와 달라진 점은 ⚠️로 표시한다.

## 왜

관심 단지·관심 지역 같은 2차 기능은 로그인한 사용자가 있어야 한다. 이번에는 계정의 뼈대만 만든다.

## 이번 범위

- 소셜 로그인·가입: 네이버, 카카오, 구글 ⚠️(명세는 카카오·네이버만)
- 가입 마무리(두 단계 가입) ⚠️
- 토큰 갱신(rotation), 로그아웃
- 내 정보 보기·닉네임 수정·회원 탈퇴

## 하지 않는 것

- 알림 동의 변경(`PUT /users/me/consents`), 학습 기록 동기화, 관심 등록, 푸시 토큰 — 다음 PR
- 소유 확인·매물 수(`verified_ownership_count`, `active_listing_count`)는 0으로 내려 둔다(3차 기능, 표만 아직 없음)
- 본인인증(CI) — 기능명세 §7 #16에서 정할 때까지 보류
- 이메일·비밀번호 가입

## 이렇게 되면 실패다

- 소셜 토큰·인가 코드를 검증하지 않고 받은 값을 믿는다(남의 계정으로 로그인됨).
- refresh token을 평문으로 저장하거나, 웹 응답 본문에 refresh token을 넣는다.
- 탈퇴했는데 닉네임·소셜 식별자가 남는다.
- 비밀값(클라이언트 시크릿, JWT 서명 키)이 코드나 커밋에 들어간다.

## 흐름

### 1. 로그인 `POST /auth/oauth/{provider}` (`provider: naver|kakao|google`)

- **앱:** `{ "access_token": "소셜 SDK 토큰" }` → 서버가 **그 토큰이 우리 앱에 발급된 것인지** 먼저 확인하고(다른 앱이 받은 사용자 토큰으로 로그인하는 토큰 대체 공격 방지), 사용자 정보 API로 소셜 사용자 ID를 얻는다.
  - 카카오: `GET https://kapi.kakao.com/v1/user/access_token_info`의 `app_id`가 `PUGURIN_KAKAO_APP_ID`와 같을 때만 `GET https://kapi.kakao.com/v2/user/me`. 앱 ID 설정이 없으면 카카오 앱 로그인은 `503`
  - 구글: ID 토큰(JWT)이면 공개키로 서명·`iss`·만료와 `aud`를 확인하고, access token이면 `https://oauth2.googleapis.com/tokeninfo`의 `aud` 또는 `azp`를 확인한 뒤 `https://openidconnect.googleapis.com/v1/userinfo`. 받는 클라이언트 ID = 웹 클라이언트 ID(`PUGURIN_GOOGLE_CLIENT_ID`) + 앱 클라이언트 ID 목록(`PUGURIN_GOOGLE_CLIENT_IDS`)
  - 네이버: 앱 토큰이 우리 앱 것인지 확인할 공식 API를 찾지 못했다. 그래서 **네이버는 웹 인가 코드만** 받고 앱 토큰 요청은 `400 VALIDATION_ERROR`(`field: "access_token"`, 웹 로그인 안내). 앱에서 네이버 로그인은 웹 인가 코드 흐름(브라우저 + 앱 스킴 redirect_uri)으로 한다
- **웹(`X-Client: web`):** `{ "code", "redirect_uri", "state", "code_verifier"? }` → `redirect_uri`가 허용 목록과 정확히 같아야 한다(아니면 400). 서버가 클라이언트 시크릿으로 코드를 토큰으로 바꾼 뒤 위처럼 사용자 정보를 얻는다. `code_verifier`가 오면 PKCE로 함께 보낸다(지원하는 제공자만).
  - `state`는 웹이 만들어 콜백에서 확인한다(명세 그대로). 서버는 state를 검증하지 않는다. 단 **네이버 토큰 교환 API는 `state`를 필수로 받으므로 네이버에만 넘긴다**(카카오·구글에는 넘기지 않는다).
  - 웹 인가 코드는 우리 클라이언트 시크릿으로 교환하므로 토큰이 우리 앱 것인지 따로 확인하지 않는다.
- **가입한 사람:** `{ is_new_user: false, access_token, refresh_token(앱만), user }`. 웹은 refresh token을 `HttpOnly; Secure; SameSite=Lax` 쿠키로만 준다.
- **처음인 사람 ⚠️:** 토큰 대신 `{ is_new_user: true, signup_token, profile: { nickname } }`. 가입 토큰은 10분, 1회용.
- 소셜 확인 실패 → `401 UNAUTHORIZED`.

### 2. 가입 마무리 `POST /auth/signup` ⚠️ (새로 추가)

- `{ signup_token, nickname, agreements: { terms, privacy, age14, marketing } }`
- `terms`·`privacy`·`age14` 중 하나라도 false → `400 VALIDATION_ERROR`(`field`에 항목 이름). `age14` ⚠️는 만 14세 미만 법정대리인 동의 때문에 새로 넣는다.
- 가입 토큰이 만료됐거나 이미 쓴 것 → `401 UNAUTHORIZED`
- 같은 소셜 계정이 그 사이에 이미 가입했으면 → 그 계정으로 로그인 응답(중복 가입하지 않음).
- 응답은 1의 가입한 사람 응답과 같다. `marketing`은 `marketing_push`와 `marketing_consented_at`에 저장한다.

### 3. 닉네임 규칙

2~12자, 한글·영문·숫자만(앞뒤 공백 제거). 프론트와 같은 규칙이다. **중복은 허용한다**(사용자를 식별하는 값이 아니라 표시 이름이라서). 내 판단이며, 막기로 하면 `409 CONFLICT`로 바꾼다.

### 4. 토큰

- **access token:** JWT(HS256), 30분. `sub`=사용자 ID, `role`, `exp`, `iat`. 서명 키는 `PUGURIN_JWT_SECRET`(32바이트 이상, 없으면 인증 API가 503).
- **refresh token:** 추측할 수 없는 임의 문자열, 14일. DB에는 SHA-256 해시만 저장한다. 갱신할 때마다 새로 발급하고 이전 것은 폐기한다(같은 `family_id`로 잇는다).
- **재사용 감지:** 이미 폐기된 refresh token이 오면 그 사용자의 모든 refresh token을 폐기하고 `401`.
- **가입 토큰:** 임의 문자열, 10분, 1회용. DB에 해시와 소셜 식별자(제공자·소셜 사용자 ID·프로필 닉네임)를 잠깐 저장하고 쓰면 지운다.
- `POST /auth/refresh`: 앱은 본문 `{ refresh_token }`, 웹은 쿠키(`X-Client: web` 필수).
- 인증이 필요한 API는 `Authorization: Bearer <access token>`. 없거나 틀리거나 만료 → `401`.

### 5. 로그아웃 `POST /auth/logout`

그 refresh token(앱은 본문, 웹은 쿠키)을 폐기하고 웹 쿠키를 지운다. 푸시 토큰 해제는 푸시가 생기면 붙인다.

### 6. 내 정보

- `GET /users/me`: 명세 응답에 `provider` ⚠️를 더한다. `consents`·`settings`는 명세 모양 그대로 기본값으로 내려 둔다.
- `PATCH /users/me`: `nickname`(규칙 3), `settings`.

### 7. 탈퇴 `DELETE /users/me`

- 한 트랜잭션에서: 소셜 연결 행 삭제, 닉네임을 "탈퇴한 사용자"로, 마케팅 동의 등 개인 항목 비우기, `deleted_at` 기록, 모든 refresh token 폐기.
- 같은 소셜 계정으로 다시 가입하면 새 사용자로 만든다(이전 기록과 잇지 않는다).

## 저장 (마이그레이션 `0011_auth`)

- `users`: id(uuid), nickname, role(`buyer`|`admin`), provider, marketing_push, marketing_consented_at, settings(jsonb), agreed_at, created_at, deleted_at
- `user_identities`: provider, provider_user_id, user_id — (provider, provider_user_id) 유일. 탈퇴하면 지운다.
- `refresh_tokens`: id, user_id, family_id, token_hash(유일), device_id, created_at, expires_at, revoked_at
- `signup_tokens`: token_hash, provider, provider_user_id, profile_nickname, expires_at

## 설정 (`.env`, 키는 사용자가 직접 넣는다)

`PUGURIN_JWT_SECRET`, `PUGURIN_KAKAO_REST_KEY`·`PUGURIN_KAKAO_CLIENT_SECRET`·`PUGURIN_KAKAO_APP_ID`(앱 로그인), `PUGURIN_NAVER_CLIENT_ID`·`PUGURIN_NAVER_CLIENT_SECRET`, `PUGURIN_GOOGLE_CLIENT_ID`(웹 클라이언트)·`PUGURIN_GOOGLE_CLIENT_SECRET`·`PUGURIN_GOOGLE_CLIENT_IDS`(앱 클라이언트 목록, 쉼표 구분), `PUGURIN_AUTH_REDIRECT_URIS`(쉼표 구분, 정확히 일치). 제공자 키가 없으면 그 제공자는 `503`(가짜 응답을 내지 않는다).

- 카카오는 REST 키가 있으면 켜지고, 앱 토큰 로그인에는 `PUGURIN_KAKAO_APP_ID`도 있어야 한다.
- 구글은 `PUGURIN_GOOGLE_CLIENT_ID`나 `PUGURIN_GOOGLE_CLIENT_IDS` 중 하나가 있으면 켜진다. 웹 인가 코드 로그인은 `PUGURIN_GOOGLE_CLIENT_ID`와 시크릿이 둘 다 있어야 한다. 앱 토큰은 두 설정에 있는 ID 중 어느 것에 발급됐어도 받는다.

## 웹과 CORS

웹(https://pugurin.netlify.app)은 Netlify의 `/api` 프록시로 백엔드를 부르므로 **같은 출처**다. 그래서 CORS 미들웨어가 없고, refresh 쿠키는 `SameSite=Lax`로 충분하다. 웹을 다른 출처에서 직접 부르게 되면 `allow_credentials` CORS(허용 출처 목록)와 쿠키 정책을 다시 정한다.

## 프론트가 맞춰야 할 점

- `marketing_push`는 사용자 응답의 최상위가 아니라 **`user.consents.marketing_push`** 에 있다(명세 §8 모양). 프론트 `User` 타입(`authTypes.ts`)의 최상위 `marketing_push`를 이 위치로 읽어야 한다.
- 네이버는 앱에서도 웹 인가 코드 흐름으로 로그인한다(앱 토큰은 400).
- 구글 앱 로그인은 ID 토큰을 보내는 것을 권한다. 앱 클라이언트 ID는 서버 `PUGURIN_GOOGLE_CLIENT_IDS`에 있어야 한다.
- `state`는 웹이 만들고 콜백에서 확인한다(서버가 만들지 않는다).

## 테스트

- 소셜 확인은 어댑터 인터페이스 뒤에 두고, 테스트는 가짜 어댑터로 키 없이 돈다(공통 규칙 1).
- 토큰 발급·검증·만료, rotation, 재사용 감지, 두 단계 가입, 동의 누락, 닉네임 규칙, 웹 쿠키, 탈퇴 익명화를 API 테스트로 확인한다. 저장은 실제 PostGIS 테스트(Docker 없으면 건너뜀).

## 내 판단으로 정한 것

- 닉네임 중복 허용
- JWT HS256(서버가 하나라 공개키 서명이 필요 없음)
- 가입 토큰을 DB에 저장(Redis가 없는 환경에서도 돌게)
- 소유 확인·매물 수는 0으로 내림
