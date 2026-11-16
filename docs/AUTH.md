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

- **앱:** `{ "access_token": "소셜 SDK 토큰" }` → 서버가 그 제공자의 사용자 정보 API로 토큰을 확인하고 소셜 사용자 ID를 얻는다.
  - 카카오 `GET https://kapi.kakao.com/v2/user/me`, 네이버 `GET https://openapi.naver.com/v1/nid/me`, 구글은 SDK가 준 ID 토큰을 받는다면 `aud`·`iss`·서명을 확인하고, access token이면 `https://openidconnect.googleapis.com/v1/userinfo`
- **웹(`X-Client: web`):** `{ "code", "redirect_uri", "state", "code_verifier"? }` → `redirect_uri`가 허용 목록과 정확히 같아야 한다(아니면 400). 서버가 클라이언트 시크릿으로 코드를 토큰으로 바꾼 뒤 위처럼 사용자 정보를 얻는다. `code_verifier`가 오면 PKCE로 함께 보낸다(지원하는 제공자만).
  - `state`는 웹이 만들어 콜백에서 확인한다(명세 그대로). 서버는 받은 값을 제공자 요청에 넘기지 않는다.
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

## 저장 (마이그레이션 `0012_auth`)

- `users`: id(uuid), nickname, role(`buyer`|`admin`), provider, marketing_push, marketing_consented_at, settings(jsonb), agreed_at, created_at, deleted_at
- `user_identities`: provider, provider_user_id, user_id — (provider, provider_user_id) 유일. 탈퇴하면 지운다.
- `refresh_tokens`: id, user_id, family_id, token_hash(유일), device_id, created_at, expires_at, revoked_at
- `signup_tokens`: token_hash, provider, provider_user_id, profile_nickname, expires_at

## 설정 (`.env`, 키는 사용자가 직접 넣는다)

`PUGURIN_JWT_SECRET`, `PUGURIN_KAKAO_REST_KEY`·`PUGURIN_KAKAO_CLIENT_SECRET`, `PUGURIN_NAVER_CLIENT_ID`·`PUGURIN_NAVER_CLIENT_SECRET`, `PUGURIN_GOOGLE_CLIENT_ID`·`PUGURIN_GOOGLE_CLIENT_SECRET`, `PUGURIN_AUTH_REDIRECT_URIS`(쉼표 구분, 정확히 일치). 제공자 키가 없으면 그 제공자는 `503`(가짜 응답을 내지 않는다).

## 테스트

- 소셜 확인은 어댑터 인터페이스 뒤에 두고, 테스트는 가짜 어댑터로 키 없이 돈다(공통 규칙 1).
- 토큰 발급·검증·만료, rotation, 재사용 감지, 두 단계 가입, 동의 누락, 닉네임 규칙, 웹 쿠키, 탈퇴 익명화를 API 테스트로 확인한다. 저장은 실제 PostGIS 테스트(Docker 없으면 건너뜀).

## 내 판단으로 정한 것

- 닉네임 중복 허용
- JWT HS256(서버가 하나라 공개키 서명이 필요 없음)
- 가입 토큰을 DB에 저장(Redis가 없는 환경에서도 돌게)
- 소유 확인·매물 수는 0으로 내림
