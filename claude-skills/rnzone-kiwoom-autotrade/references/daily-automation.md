# 매일 무인 자동 실행 (Windows 작업 스케줄러)

RN존 리포트 계산부터 키움 실주문까지 사람 개입 없이 매일 자동으로 나가도록
등록하는 절차. **실제 자금이 매일 자동으로 움직인다** — 등록 전 아래 전제 조건을
반드시 충족할 것.

## 전제 조건

1. 이 PC가 실제로 매일 자동 실행 시각에 **켜져 있고 로그인 상태**여야 한다
   (화면 잠금은 괜찮지만 로그아웃은 안 됨).
2. 이 PC의 공인 IP가 키움 REST API 포털(계좌 App Key 관리 → IP 등록)에
   등록되어 있어야 한다. 가정용 인터넷은 IP가 바뀔 수 있으니, 바뀌면
   `openapi.kiwoom.com`에서 재등록해야 한다는 점을 알아둘 것.
3. `.env`가 `claude-skills/rnzone-kiwoom-autotrade/` 폴더에 있고, `kiwoomcli`/`kwcli`
   설치가 끝나 있어야 한다.
4. **dry-run과 실제 실주문 최소 1회를 사람이 직접 확인한 뒤에만** 무인 등록으로
   넘어간다.

## 1단계: run_daily.bat 확인

저장소의 `claude-skills/rnzone-kiwoom-autotrade/run_daily.bat`가 이미 준비되어
있다. 이 파일은:

- `.env`를 읽어 환경변수로 로드
- `KIWOOM_LIVE_TRADING=YES` + `--live`로 실주문 실행 (이중 안전장치 그대로 유지)
- 주문 1건 상한 `--max-order-usd` 적용(파일 안에서 값 조정 가능)
- 모든 출력을 같은 폴더의 `daily_run.log`에 날짜별로 누적 기록

상한 값을 바꾸려면 `run_daily.bat`을 메모장으로 열어 아래 줄의 숫자만 수정한다.

```bat
python scripts\kiwoom_autotrade.py --live --max-order-usd 50
```

## 2단계: 수동으로 한 번 실행해 확인

작업 스케줄러에 등록하기 전에, 더블클릭 또는 CMD에서 직접 한 번 실행해
정상적으로 로그가 쌓이는지 확인한다.

```
cd C:\Users\parki\House\claude-skills\rnzone-kiwoom-autotrade
run_daily.bat
type daily_run.log
```

## 3단계: 작업 스케줄러 등록

CMD(관리자 권한 필요 없음)에서 아래 명령을 실행한다. 경로는 실제 저장소 위치에
맞게 수정할 것. 매일 오전 11시(한국시간, 미국 증시 마감 이후·개장 이전)에
실행하도록 설정한다.

```
schtasks /create /tn "RNZone_Kiwoom_AutoTrade" /tr "\"C:\Users\parki\House\claude-skills\rnzone-kiwoom-autotrade\run_daily.bat\"" /sc daily /st 11:00
```

등록 확인:

```
schtasks /query /tn "RNZone_Kiwoom_AutoTrade"
```

당장 한 번 테스트로 돌려보고 싶으면:

```
schtasks /run /tn "RNZone_Kiwoom_AutoTrade"
```

## 4단계: 중지/삭제

```
schtasks /delete /tn "RNZone_Kiwoom_AutoTrade" /f
```

## 운영 중 확인 습관

- 매일이 아니더라도 최소 주 1~2회는 `daily_run.log`를 열어 주문이 계획대로
  나갔는지, 에러가 없는지 확인할 것 (IP가 바뀌었거나 키움 쪽 스펙이 바뀌면
  조용히 계속 실패할 수 있다).
- 매도(목표가·본절가·기간청산)는 여전히 자동화되어 있지 않다 — 진입한 종목은
  사람이 계좌를 보고 직접 관리해야 한다.
- 계좌 예수금이 소진되면 이후 신호는 수량 0으로 건너뛴다(에러 아님, 정상 동작).
