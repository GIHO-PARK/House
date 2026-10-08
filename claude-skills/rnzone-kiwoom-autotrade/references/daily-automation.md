# 매일 무인 자동 실행 (집 PC + Windows 작업 스케줄러)

RN존 신호 계산부터 키움 실주문(매수·매도)까지 사람 개입 없이 매일 자동으로 나가도록
등록하는 절차. **실제 자금이 매일 자동으로 움직인다** — 등록 전 아래 전제 조건을
반드시 충족할 것.

## 왜 하루 한 번이면 되나

키움 미국주식 지정가 주문은 **당일만 유효**하다. 실행기는 매일 한 번, 미국 정규장이
열린 직후에 그날의 1·2·3차 매수선과 매도가(목표·본절·기간청산)를 지정가로 건다.
주문을 넣은 뒤에는 PC가 다시 잠들어도 키움 서버에서 체결된다. PC는 상시 켜 둘 필요
없이 **절전 상태**면 되고, 작업 스케줄러가 실행 시각에 깨운다.

실행 시각: **월~금 한국시간 23:45** — 미국 정규장 개장(서머타임 22:30, 겨울 23:30)
이후라 일 년 내내 정규장 중에 주문이 들어간다. 이 시각에는 야후가 '진행 중인 오늘
봉'을 주지만 리포트 스크립트가 자동으로 버리고 전날 종가 기준으로 계산한다.

## 전제 조건

1. PC가 실행 시각에 **절전 또는 켜진 상태 + 로그인 상태**여야 한다
   (화면 잠금·절전은 괜찮음. 로그아웃·완전 종료·최대 절전(하이버네이트)은 안 됨).
2. 이 PC의 공인 IP가 키움 REST API 포털(openapi.kiwoom.com → App Key 관리 → IP 등록)에
   등록되어 있어야 한다. 가정용 인터넷은 IP가 바뀔 수 있다 — 바뀌면 주문이 조용히
   실패하므로 `daily_run.log`를 주기적으로 확인하고, 실패하면 IP를 재등록한다.
3. `.env`가 `claude-skills/rnzone-kiwoom-autotrade/` 폴더에 있고, Python 3.13+와
   `kwcli` 설치·인증이 끝나 있어야 한다([setup.md](setup.md)).
4. **dry-run과 실제 실주문 최소 1회를 사람이 직접 확인한 뒤에만** 무인 등록으로 넘어간다.
5. 영웅문에서 **미수 거래 불가(현금 주문만)** 로 설정해 둔다 — 미수·반대매매 원천 차단.
6. 운용금은 **달러로 미리 환전**해 둔다(원화주문 자동환전은 거래마다 환전비용이 든다).
   비상금 1,000만원은 원화로 둬도 된다. 원화주문을 꼭 쓰려면 `.env`에
   `KIWOOM_KRW_ORDER=YES`를 넣는다.

## .env 예시

```
KIWOOM_MODE=real
APP_KEY=발급받은_운영_App_Key
APP_SECRET=발급받은_운영_App_Secret
KIWOOM_MAX_ORDER_USD=7000
```

`kiwoomcli setup`으로 인증했다면 APP_KEY/APP_SECRET 줄은 빼고 나머지만 둔다.
`KIWOOM_MAX_ORDER_USD`는 주문 1건 상한(USD) — 처음 몇 주는 낮게(예: 500) 두고
검증한 뒤 올린다. 7000이면 가장 큰 계획 주문(지수형 3차 900만원)까지 통과한다.

## 1단계: 수동으로 한 번 실행해 확인

```
cd C:\Users\parki\House\claude-skills\rnzone-kiwoom-autotrade
python scripts\kiwoom_autotrade.py          ← dry-run: 계좌를 읽고 주문 계획만 출력
run_daily.bat                                ← 실주문(상한 적용). 결과는 daily_run.log
type daily_run.log
```

## 2단계: 절전 해제 + 작업 스케줄러 등록

**① 전원 옵션에서 절전 해제 타이머 허용** (한 번만)

제어판 → 전원 옵션 → 사용 중인 전원 관리 옵션의 "설정 변경" → "고급 전원 관리 옵션 설정 변경"
→ 절전 → **절전 모드 해제 타이머 허용: 사용**.

**② PowerShell에서 작업 등록** (관리자 권한 불필요, 경로는 실제 위치로 수정)

```powershell
$dir = "C:\Users\parki\House\claude-skills\rnzone-kiwoom-autotrade"
$action = New-ScheduledTaskAction -Execute "$dir\run_daily.bat" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 23:45
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "RNZone_Kiwoom_AutoTrade" -Action $action -Trigger $trigger -Settings $settings
```

- `-WakeToRun`: 절전 중이면 깨워서 실행
- `-StartWhenAvailable`: 그 시각에 PC가 꺼져 있었다면 켜지는 즉시 실행

확인 · 즉시 테스트 · 삭제:

```powershell
Get-ScheduledTask -TaskName "RNZone_Kiwoom_AutoTrade" | Get-ScheduledTaskInfo
Start-ScheduledTask -TaskName "RNZone_Kiwoom_AutoTrade"
Unregister-ScheduledTask -TaskName "RNZone_Kiwoom_AutoTrade" -Confirm:$false
```

## 실행기가 매일 하는 일

1. 리포트 계산(`rnzone_report.py --json`) → 종목별 매수선·주문금액
2. 키움 잔고·미체결·예수금 조회 → 장부(`scripts/positions.json`)와 맞춤
   (어제 체결분은 차수 올림, 잔고에서 사라진 종목은 청산으로 기록)
3. 주문(우선순위 순서)
   - 매도: 기간청산(레버 42일·본주 63일, 전날 종가 -3% 지정가) / 2차 이후 본절(평단) / 목표(평단 +20%)
   - 본주 체결분 → 주배당 ETF 적립(전날 종가 +1% 지정가)
   - 보유 종목의 다음 차수(2차가 3차보다 먼저)
   - 신규 1차: 매수 대기 종목 중 매수선에 가까운 순, 동시 보유 7종목(대기 주문 포함),
     반도체·기술 계열 2종목, 그 종목의 2차까지 낼 현금이 있을 때만
   - 트레이딩 평가손이 운용금의 -30% 이하면 신규 1차 중단(회로차단기)
4. 같은 날 다시 실행해도 같은 주문은 다시 내지 않는다(이미 걸린 주문은 유지, 가격이
   달라진 우리 종목 주문은 취소 후 새로 냄). **장부 밖 종목(직접 산 종목)은 건드리지 않는다.**

## 운영 중 확인 습관

- 최소 주 1~2회 `daily_run.log` 확인 — IP 변경·토큰 만료·키움 스펙 변경으로 조용히
  실패할 수 있다.
- 키움 앱에서 체결·잔고를 직접 사고팔았다면 장부와 어긋날 수 있다. 장부를 초기화하려면
  `scripts\positions.json`을 지우면 된다(보유 종목은 '장부 밖 종목'으로 취급되어
  자동매매가 건드리지 않게 된다).
- 분배금 재투자(섹터 모멘텀)는 아직 수동이다 — 아침 리포트의 '이번 주 재투자 대상'을 참고.
- 미국 휴장일에는 주문이 거부되거나 체결되지 않고 지나간다(정상).
