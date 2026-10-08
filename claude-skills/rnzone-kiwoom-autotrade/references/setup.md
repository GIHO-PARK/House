# 설치 · 인증 · 네트워크 설정

## 1. Python 3.13+ 준비

공식 키움 REST API 패키지(`kwcli`, import 이름 `kiwoom`)는 PyPI 메타데이터에
`Requires-Python >= 3.13`으로 고정되어 있다. 3.13 미만에서는 `pip install kwcli`
자체가 실패한다(버전 불일치로 배포판을 찾지 못함). 실행할 PC/서버에 3.13 이상을
준비할 것.

```bash
python3 --version   # 3.13.x 이상이어야 함
```

## 2. 키움 REST API 패키지 설치

```bash
pip install kwcli
# 또는 uv 사용 시: uv add kwcli
```

## 3. App Key / Secret 발급 및 인증

1. https://openapi.kiwoom.com 에서 로그인 → OpenAPI 사용 신청 → 앱 등록.
2. **운영(real)** 키를 발급받는다(모의투자와 키가 다르다). 실거래 서버로 바로
   가기로 했으므로 운영 키가 필요하다.
3. 인증 설정(권장, 자격 증명 저장소 사용):
   ```bash
   pip install kwcli  # 위와 동일, 최초 1회
   kiwoomcli setup    # 서버 선택에서 [2] real 선택, App Key/Secret 입력
   kiwoomcli auth status
   ```
4. 또는 환경변수 방식(자격 증명 저장소를 쓸 수 없는 서버용):
   ```bash
   export KIWOOM_MODE=real
   export APP_KEY=발급받은_운영_App_Key
   export APP_SECRET=발급받은_운영_App_Secret
   ```
   `kiwoomcli setup`으로 이미 인증했다면 이 환경변수들을 함께 설정하지 말 것
   (충돌한다).

## 4. 네트워크 허용 (클라우드/Claude Code on the web 환경)

이 저장소를 클라우드 세션(Claude Code on the web)에서 실행하는 경우, 기본
네트워크 정책은 아래 두 호스트를 막는다 — `openapi.kiwoom.com`(문서 포털)은
열려 있지만 **실제 REST API 서버는 별도 도메인**이라 기본값으로는 닫혀 있다.

- `api.kiwoom.com` — 운영(실거래) REST API
- `mockapi.kiwoom.com` — 모의투자 REST API(모의투자를 함께 쓸 경우)

허용 방법: claude.ai/code 에서 해당 세션/루틴 편집(✏️) → 환경 선택 → "+ 환경
추가" → 네트워크 액세스 "사용자 정의" → 허용 도메인에 위 호스트를 추가 →
환경 생성 → 저장. rnzone-report 스킬이 야후 파이낸스 도메인에 대해 요구하는
것과 같은 절차다. 이 설정 없이는 `kiwoom_autotrade.py`가 토큰 발급 단계에서
막힌다.

로컬 PC나 자체 서버에서 실행한다면 이 단계는 필요 없다.

## 5. Dry-run → Live 전환

`kiwoom_autotrade.py`는 기본이 무조건 dry-run이다. 실주문을 넣으려면 **둘 다**
필요하다:

```bash
export KIWOOM_LIVE_TRADING=YES
python3 scripts/kiwoom_autotrade.py --live
```

권장: 처음 며칠은 `--max-order-usd`로 1건당 상한을 걸어 소액으로 검증할 것.

```bash
python3 scripts/kiwoom_autotrade.py --live --max-order-usd 50
```

## 6. 아직 안 된 것

- 분배금 섹터모멘텀 재투자는 수동이다(아침 리포트의 '이번 주 재투자 대상' 참고).
- 장부(`scripts/positions.json`)는 이 실행기가 낸 주문만 추적한다. 키움 앱에서 직접
  사고팔았다면 장부를 정리해야 한다.
- 무인 실행은 [daily-automation.md](daily-automation.md) — 집 PC 작업 스케줄러(절전 해제) 기준.
