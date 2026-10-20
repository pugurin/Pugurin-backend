# 백엔드 배포 가이드

리눅스 서버 1대에 지금 Docker 구성을 그대로 올린다. 구성은 `compose.yaml` + 운영용 덮어쓰기 `compose.prod.yaml`이다.

```
인터넷 ──▶ caddy (80·443, HTTPS 자동) ──▶ api ──▶ db (PostGIS)
                                            └──▶ redis
           worker (매일 04시 수집·적재) ──▶ db
```

- 밖으로 열리는 포트는 **caddy의 80·443뿐**이다. db·redis·api는 서버 안에서만 닿는다.
- `/internal/*`(readiness)는 caddy가 밖에서 막는다(404).
- worker가 공공데이터포털에서 직접 수집해 DB에 넣는다. 그래서 내 PC의 `.cache`를 옮길 필요가 없다.

## 0. 서버 고르기

| 사양 | 최소 | 권장 |
|---|---|---|
| CPU / 메모리 | 2코어 / 4GB | 2코어 / 8GB 이상 |
| 디스크 | 30GB | 50GB |
| OS | Ubuntu 22.04·24.04 (x86·ARM 모두 됨) | |

**무료로 쓰려면**
- **Oracle Cloud Always Free:** ARM(Ampere A1) VM을 무료로 준다.
  - 2026년에 무료 한도가 줄었다는 보고가 있다(4코어/24GB → 2코어/12GB). 만들기 전에 [공식 Always Free 문서](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm)에서 현재 한도를 확인하고, 그 **안에서만** 만든다. 한도를 넘으면 유료로 바뀌거나 인스턴스가 정지된다.
  - 가입할 때 카드 인증이 필요하다(무료 등급만 쓰면 청구되지 않음).
  - 인기 지역은 "Out of capacity"로 생성이 실패할 수 있다. 다른 가용 도메인(AD)을 고르거나 시간을 두고 다시 시도한다.
  - CPU를 오래 거의 안 쓰면 회수될 수 있다고 알려져 있다. 이 구성은 worker가 매일 돌아서 완전한 유휴 상태는 아니다.
- **GitHub Student Developer Pack:** 학생 인증이 되면 클라우드 크레딧(DigitalOcean·Azure 등)을 받을 수 있다. 크레딧이 끝나면 유료다.

계정 만들기·결제수단 등록·서버 생성은 직접 한다.

## 1. 서버 준비

방화벽에서 **TCP 80, 443**을 연다. SSH(22)는 기본으로 열려 있다.

> **Oracle Cloud는 두 군데를 다 열어야 한다.**
> 1. 콘솔의 Security List(또는 NSG)에 80·443 수신 규칙을 추가한다.
> 2. Ubuntu 이미지 안의 iptables도 연다.
> ```bash
> sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
> sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
> sudo netfilter-persistent save
> ```

Docker를 설치한다.
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 로그아웃 후 다시 접속해야 적용
docker compose version          # v2.24 이상이어야 한다
```

## 2. 코드 받기

```bash
git clone https://github.com/pugurin/Pugurin-backend.git
cd Pugurin-backend
```
비공개 레포라면 서버에서 `gh auth login`을 하거나 배포 키(SSH)를 등록한다.

## 3. 설정 파일 두 개

**① 레포 루트 `.env`:** DB 비밀번호와 도메인
```bash
cp deploy/.env.example .env
openssl rand -hex 24            # 나온 값을 PUGURIN_DB_PASSWORD에 넣는다
nano .env
```
- `PUGURIN_DB_PASSWORD`는 **처음 띄우기 전에 정하고 바꾸지 않는다.** DB는 처음 만들 때의 비밀번호로 고정된다.
- `PUGURIN_DOMAIN`은 비워 두면 `http://서버IP`로 뜬다.

**② `backend/.env`:** API 키
```bash
cp backend/.env.example backend/.env
nano backend/.env
```
아래 세 개를 채운다.
- `PUGURIN_DATA_GO_KR_TRADE_KEY`
- `PUGURIN_DATA_GO_KR_RENT_KEY`
- `PUGURIN_KAKAO_REST_KEY`

나머지는 compose가 정한다. 데이터 모드는 DB를 읽는 `auto`다.

두 파일 모두 git에 올리지 않는다(`.gitignore`). 채팅이나 이슈에도 붙여 넣지 않는다.

## 4. 띄우기

명령이 길어서 별칭을 만들어 두면 편하다.
```bash
echo "alias dc='docker compose -f compose.yaml -f compose.prod.yaml'" >> ~/.bashrc && source ~/.bashrc
dc up -d --build
dc ps                            # db·redis·api·worker·caddy가 Up(healthy), migrate는 Exited(0)
```

**확인**
```bash
curl http://서버IP/api/v1/health                 # {"status":"ok"} 그리고 X-Data-Mode: real
dc exec api python -m app.db status              # 스키마 버전, 거래 건수, 마지막 적재
dc logs -f worker                                # 수집 진행 상황
```

## 5. 처음 데이터 채우기 (백필)

worker는 시작하자마자 한 번 돌고, 이후 매일 04시(KST)에 돈다.
- 처음에는 `PUGURIN_BACKFILL_MONTHS`(기본 60개월)만큼 받는다.
  - 실거래가 API를 약 6,700회 부른다. 공공데이터포털 개발계정 한도는 API마다 하루 1만 회라서 대개 하루 안에 끝난다.
  - 카카오 지오코딩은 새로 나온 주소만 부른다.
- 한도에 걸리면 멈췄다가 다음 날 실행에서 남은 부분부터 이어 받는다.
- 신고 기한이 지난 달은 한 번 받으면 다시 받지 않는다. 그래서 이후로는 매일 최근 3개월만 다시 받는다.

**호출을 아끼려면** 내 PC에 이미 받아 둔 실거래가 원본(`.cache/molit`)을 서버로 옮겨 worker 볼륨에 넣는다. 옮긴 만큼 다시 받지 않는다.
```bash
# 내 PC에서
scp -r backend/.cache/molit 사용자@서버IP:~/molit
# 서버에서 (dc up 이후)
dc cp ~/molit worker:/app/.cache/molit
dc exec -u root worker chown -R app /app/.cache   # 복사한 파일을 worker 사용자 소유로
dc restart worker
```
- 카카오 지오코딩 결과(`.cache/geocode.json`)는 옮기지 않는다. 약관 확인 전이라 밖으로 내보내지 않기로 했다(`backend/AGENTS.md`). 서버의 worker가 직접 다시 조회한다.

## 6. 도메인과 HTTPS

1. 도메인 관리 화면에서 **A 레코드**를 서버 IP로 지정한다. 예: `api.pugurin.kr → 서버IP`
2. `.env`에 `PUGURIN_DOMAIN=api.pugurin.kr`를 넣고 `dc up -d`를 실행한다.
3. caddy가 Let's Encrypt 인증서를 자동으로 받고 갱신한다. 이때 80·443이 열려 있어야 한다.
4. 앱의 서버 주소를 `https://api.pugurin.kr/api/v1`로 바꾼다.

## 7. 업데이트

```bash
git pull
dc up -d --build                 # migrate가 새 마이그레이션을 먼저 적용한 뒤 api가 뜬다
```

## 8. 백업

DB를 매일 덤프한다. 7일치를 남기는 예시다.
```bash
mkdir -p ~/backups
crontab -e
# 아래 한 줄 추가 (매일 05:00)
0 5 * * * cd ~/Pugurin-backend && docker compose -f compose.yaml -f compose.prod.yaml exec -T db pg_dump -U pugurin -Fc pugurin > ~/backups/pugurin-$(date +\%a).dump
```
복원할 때:
```bash
dc exec -T db pg_restore -U pugurin -d pugurin --clean --if-exists < ~/backups/pugurin-Mon.dump
```
거래 데이터는 원천에서 다시 받을 수 있다. 다만 2차부터는 사용자 데이터가 생기므로 백업을 서버 밖(다른 저장소)에도 둔다.

## 9. 문제 해결

| 증상 | 확인 |
|---|---|
| `curl`이 응답 없음 | 방화벽 80·443 (Oracle은 Security List와 iptables 둘 다), `dc ps`에서 caddy 상태 |
| caddy가 계속 재시작 | `dc logs caddy`. 도메인 DNS가 아직 서버를 가리키지 않으면 인증서 발급이 실패한다 |
| api가 unhealthy | `dc logs api`, `dc logs migrate`. DB 비밀번호를 바꿨다면 아래 참고 |
| 지도에 아무것도 없음 | `dc exec api python -m app.db status`에서 거래 0건이면 `dc logs worker`. 키가 없으면 worker가 안내를 남기고 멈춘다 |
| worker "호출 한도" | 정상. 다음 날 이어서 받는다 |

**DB 비밀번호를 바꿔야 할 때:** 먼저 DB 안에서 바꾸고 `.env`를 맞춘다.
```bash
dc exec db psql -U pugurin -c "ALTER USER pugurin PASSWORD '새비밀번호'"
nano .env      # PUGURIN_DB_PASSWORD 수정
dc up -d
```
