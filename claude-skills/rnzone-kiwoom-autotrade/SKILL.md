---
name: rnzone-kiwoom-autotrade
description: RN존 듀얼전략(rnzone-report 스킬)의 오늘 신규 진입 신호를 키움증권 REST API(실거래)로 지정가 매수 주문까지 연결한다. 사용자가 "자동매매 실행", "오늘 신호 주문 넣어줘", "키움 주문", "실주문", "드라이런 돌려줘"를 언급하면 이 스킬을 사용할 것. 리포트만 보고 싶으면 rnzone-report 스킬을 대신 쓴다.
---

# RN존 → 키움증권 실주문 실행기

rnzone-report 스킬이 계산한 "오늘 신규 진입 대상"을 그대로 받아 키움증권 REST
API(운영 서버, 미국주식)로 지정가 매수 주문을 넣는다. 신호 계산 로직은
rnzone-report와 완전히 동일한 코드를 그대로 호출한다(`rnzone_report.py --json`)
— 리포트에서 사람이 읽는 숫자와 실제 주문 숫자가 어긋날 수 없다.

## 사용 전 준비 (한 번만)

[references/setup.md](references/setup.md) 참고 — Python 3.13+, `pip install
kwcli`, App Key/Secret 발급·인증, (클라우드 세션이면) `api.kiwoom.com` 네트워크
허용까지 순서대로 안내되어 있다. 준비가 안 됐으면 먼저 이 문서로 안내한다.

## 사용법

**항상 dry-run(계획만 출력, 주문 없음)으로 먼저 실행한다:**

```bash
cd scripts
python3 kiwoom_autotrade.py
```

출력에 나온 종목·거래소·수량·금액을 사용자와 함께 확인한 뒤에만 실주문으로
전환한다. 실주문은 두 가지를 모두 충족해야 한다(이중 안전장치, 하나만 켜면
자동으로 dry-run 유지):

```bash
export KIWOOM_LIVE_TRADING=YES
python3 kiwoom_autotrade.py --live
```

1건당 상한을 걸고 싶으면 `--max-order-usd 50` 처럼 추가한다.

## 이 스킬이 하는 일 / 하지 않는 일

- **한다**: 오늘 신규 진입 신호(레버리지 ETF + 듀얼코어 본주 + 연계 WeeklyPay
  주배당 적립)를 지정가(trde_tp=00) 매수 주문으로 제출. 주문 전 실제
  주문가능수량(ust31490)으로 수량을 한 번 더 검증. 모든 시도를
  `scripts/order_log.jsonl`에 기록. 같은 날 같은 종목/단계 중복 주문 방지.
- **하지 않는다**: 매도 자동화(목표가/본절가/기간청산) — 아직 미연결, 사람이
  실제 계좌 잔고를 보고 처리해야 한다. 시장가 주문 — 항상 지정가만 사용한다
  (RN존 스킬 자체 지침: "얇은 ETF는 지정가 주문 필수"). `NEW_INVESTOR_MODE=False`
  (실보유 추적) 리포트 — 신규 진입 전용이다.

## 사용자에게 항상 전달할 것

- 이것은 **실거래(real) 서버**로 실제 자금이 움직이는 자동매매다. dry-run 결과를
  사람이 검토하기 전에는 `--live`를 권하지 말 것.
- rnzone-report 스킬의 투자 자문 아님 고지, 백테스트 한계(상승장 표본), 레버리지
  ETF 변동성 경고가 여기에도 동일하게 적용된다.
- 매도는 아직 자동화되지 않았음을 매번 상기시킨다 — 진입 후 목표가/기간청산
  관리는 사용자 몫이다.

## 커스터마이징

주문 로직은 신호 계산(rnzone_report.py)과 분리되어 있다. 슬롯/자본 배분을
바꾸려면 rnzone-report 스킬의 `CAPITAL_KRW`/`TRADE_RATIO`를 수정한다(사용자에게
반드시 확인 후). 주문 1건 상한은 `KIWOOM_MAX_ORDER_USD` 환경변수 또는
`--max-order-usd`로 조정한다.

## 매일 무인 자동 실행 (선택)

사용자가 사람 개입 없이 매일 자동으로 실주문까지 나가길 원하면
[references/daily-automation.md](references/daily-automation.md)를 따라
`run_daily.bat` + Windows 작업 스케줄러로 등록한다. 전제 조건:

- **IP가 고정된 환경에서만** 등록한다 — 키움 REST API는 App Key에 등록된 IP에서만
  인증이 통과된다. 매번 IP가 바뀌는 클라우드 세션에서는 무인 스케줄을 걸지 않는다.
- 등록 전에 **반드시 dry-run과 최소 1회 이상의 실주문을 사람이 직접 확인**한
  상태여야 한다. 검증 안 된 상태로 무인 스케줄 등록을 돕지 말 것.
- 등록 시 주문 1건 상한(`--max-order-usd`)을 반드시 사용자에게 확인해서 명시한다
  — 상한 없이 무인으로 돌리자고 하면 상한을 걸도록 권한다.
